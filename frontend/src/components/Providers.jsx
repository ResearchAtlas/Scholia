// i18n: migrated
// Providers and models (the stage walk; ticket 50): connect a provider first, then choose
// its models. Providers are grouped as Ready, Needs setup or Off. Each offers Recommended,
// All or picked models, and has a default window; under Advanced, each model shows its
// window as reported and in use, with an override and Restore, its effort steps and its
// price (slice-1 spec sections 4.4 and 8). Keys go to the credential store, never a file.
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronRight, KeyRound, Plus, Search } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, put } from '../api.js';
import { errorText } from '../text.js';
import { WINDOW_PRESETS, forgetModels, groupOf, loadModels, onCatalogChange, settingKey, useSettingsFile } from '../settings.js';
import { CommitField, Field, FileProblems, LoadState, Restore, Section, Segmented } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

const MIN_WINDOW = 4096; // the smallest supported window (backend/providers.py)
const GROUPS = ['ready', 'setup', 'off'];
const NAME = /^[a-z0-9][a-z0-9_-]{0,39}$/; // a provider's name, a bare TOML key

export function Providers() {
  const t = useT();
  const personal = useSettingsFile();
  const [list, setList] = useState(null);
  const [problem, setProblem] = useState(null);
  const [adding, setAdding] = useState(false);

  const reads = useRef(0); // only the newest read of the providers is shown
  const load = useCallback(async () => {
    const read = ++reads.current;
    try {
      const { providers } = await get('/api/providers');
      if (read !== reads.current) return;
      setList(providers);
      setProblem(null);
    } catch (error) {
      if (read === reads.current) setProblem(error instanceof ApiError ? error.code : 'internal');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // A provider or its key changed (or a change was refused because the file changed): the
  // providers and their listings are read again; keep holds a refused change's message.
  const changed = useCallback(async ({ keep = false } = {}) => {
    forgetModels();
    await Promise.all([load(), personal.reload({ keep })]);
  }, [load, personal]);

  if (!list || !personal.values) {
    return <LoadState problem={personal.problem ?? problem} onRetry={() => { setProblem(null); load(); personal.reload(); }} />;
  }
  const tables = personal.values.providers ?? {};
  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">{t('settings.page.providers')}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{t('providers.intro')}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => setAdding(true)}><Plus aria-hidden="true" />{t('providers.add')}</Button>
      </div>
      {(personal.problem || problem) && (
        <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {personal.problem === 'settings_changed' ? t('settings.changedOnDisk') : errorText(t, personal.problem ?? problem)}
        </p>
      )}
      <FileProblems problems={personal.problems} />
      {adding && <AddEndpoint existing={list.map((p) => p.name)} onCancel={() => setAdding(false)}
        onAdded={async () => { setAdding(false); await changed(); }} />}
      {GROUPS.map((group) => {
        const members = list.filter((p) => groupOf(p) === group);
        if (!members.length) return null;
        return (
          <Section key={group} title={t(`providers.group.${group}`)}>
            <div className="space-y-3">
              {members.map((provider) => (
                <ProviderCard key={provider.name} provider={provider} table={tables[provider.name] ?? {}}
                  save={personal.save} onChanged={changed} />
              ))}
            </div>
          </Section>
        );
      })}
    </div>
  );
}

function ProviderCard({ provider, table, save, onChanged }) {
  const t = useT();
  const [key, setKey] = useState('');
  const [keyProblem, setKeyProblem] = useState(null); // the key form's
  const [modelsProblem, setModelsProblem] = useState(null); // the model policy's and the default window's
  const [notice, setNotice] = useState(null);
  const [open, setOpen] = useState(false);
  const [choosing, setChoosing] = useState(false);
  const openrouter = provider.kind === 'openrouter';
  const choice = Array.isArray(table.models) ? 'pick' : table.models ?? (openrouter ? 'recommended' : 'all');
  const ready = groupOf(provider) === 'ready';

  async function saveKey(event) {
    event.preventDefault();
    setKeyProblem(null);
    try {
      const saved = await put(`/api/keys/${encodeURIComponent(provider.name)}`, { key });
      setKey('');
      setNotice(saved.warning ? t('setup.storeWarning') : null);
      await onChanged();
    } catch (error) {
      setKeyProblem(error instanceof ApiError ? error.code : 'internal');
    }
  }

  async function setChoice(next) { // one at a time: the control waits while Pick reads the listing
    setChoosing(true);
    setModelsProblem(null); // an earlier failed change's message goes with the new attempt
    try {
      if (next === 'pick') { // starts from the models offered now; a listing that failed changes nothing
        const listing = await loadModels(provider.name).catch(() => null);
        if (!listing || listing.status?.error) {
          setModelsProblem(listing?.status?.error ?? 'network');
          return;
        }
        await save({ [settingKey('providers', provider.name, 'models')]: listing.models.filter((m) => m.offered).map((m) => m.id) });
      } else {
        await save({ [settingKey('providers', provider.name, 'models')]: next });
      }
      forgetModels();
    } finally {
      setChoosing(false);
    }
  }

  return (
    <article className="rounded-xl border bg-card p-4 shadow-sm">
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h4 className="text-sm font-semibold">{openrouter ? t('providers.openrouter') : provider.name}</h4>
          <p className="truncate font-mono text-xs text-muted-foreground" title={provider.base_url}>{provider.base_url}</p>
        </div>
        <Segmented label={t('providers.onOff', { name: provider.name })} value={provider.enabled ? 'on' : 'off'}
          options={[{ value: 'on', label: t('providers.on') }, { value: 'off', label: t('providers.off') }]}
          onChange={async (value) => { // refused (the file changed): everything is read again, its message kept
            const saved = await save({ [settingKey('providers', provider.name, 'enabled')]: value === 'on' ? null : false });
            await onChanged({ keep: !saved });
          }} />
      </header>

      <form onSubmit={saveKey} className="mt-4 grid gap-1.5">
        <label htmlFor={`key-${provider.name}`} className="flex items-center gap-1.5 text-sm font-medium">
          <KeyRound className="size-3.5" aria-hidden="true" />
          {provider.has_key ? t('providers.keySaved') : t('providers.keyMissing')}
        </label>
        <div className="flex gap-2">
          <Input id={`key-${provider.name}`} type="password" autoComplete="off" spellCheck={false} value={key}
            placeholder={provider.has_key ? t('providers.replaceKey') : t('providers.pasteKey')}
            onChange={(event) => setKey(event.target.value)} className="h-9 font-mono" />
          <Button type="submit" size="sm" className="h-9" disabled={!key.trim()}>{t('common.save')}</Button>
        </div>
        {keyProblem && <p role="alert" className="text-xs text-destructive">{errorText(t, keyProblem)}</p>}
        {notice && <p role="status" className="text-xs text-warning">{notice}</p>}
      </form>

      {ready && (
        <div className="mt-5 grid gap-5">
          {modelsProblem && <p role="alert" className="text-xs text-destructive">{errorText(t, modelsProblem)}</p>}
          <Field label={t('providers.models')} hint={t(`providers.modelsHint.${choice}`)}>
            <div>
              <Segmented label={t('providers.models')} value={choice} onChange={setChoice} disabled={choosing}
                options={[...(openrouter ? [{ value: 'recommended', label: t('providers.recommended') }] : []),
                  { value: 'all', label: t('providers.all') }, { value: 'pick', label: t('providers.pick') }]} />
            </div>
          </Field>
          {choice === 'pick' && <Picker provider={provider} picked={table.models} save={save} />}
          <Field label={t('providers.defaultWindow')} htmlFor={`window-${provider.name}`} hint={t('providers.defaultWindowHint')}
            aside={<Restore disabled={table.default_window == null}
              onClick={() => save({ [settingKey('providers', provider.name, 'default_window')]: null }).then(forgetModels)} />}>
            <div className="max-w-48">
              <CommitField id={`window-${provider.name}`} type="number" min={MIN_WINDOW} step={1} inputMode="numeric"
                value={table.default_window} suggestions={WINDOW_PRESETS} allowEmpty placeholder={t('providers.windowReported')}
                onCommit={(value, error) => {
                  setModelsProblem(error ? 'window_too_small' : null);
                  if (!error) save({ [settingKey('providers', provider.name, 'default_window')]: value }).then(forgetModels);
                }} />
            </div>
          </Field>
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
            className="flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground">
            <ChevronRight className={cn('size-4 transition-transform', open && 'rotate-90')} aria-hidden="true" />
            {t('providers.perModel')}
          </button>
          {open && <PerModel provider={provider} table={table} save={save} />}
        </div>
      )}
    </article>
  );
}

// Picks the models a provider offers, from its listing.
function Picker({ provider, picked, save }) {
  const t = useT();
  const [models, setModels] = useState(null);
  const [query, setQuery] = useState('');
  useEffect(() => { // read again whenever a provider or its models change; only the newest read counts
    let newest = 0;
    const read = () => {
      const mine = ++newest;
      loadModels(provider.name).then((listing) => mine === newest && setModels(listing.models))
        .catch(() => mine === newest && setModels([]));
    };
    read();
    const stop = onCatalogChange(read);
    return () => { newest += 1; stop(); };
  }, [provider.name]);
  const chosen = useMemo(() => new Set(picked ?? []), [picked]);
  const [busy, setBusy] = useState(false); // one change at a time, each from the list as saved
  if (!models) return <p className="text-sm text-muted-foreground" role="status">{t('common.loading')}</p>;
  const shown = models.filter((m) => m.id !== 'auto' // the id means Auto, never a model (backend/providers.py)
    && `${m.id} ${m.name}`.toLowerCase().includes(query.toLowerCase())).slice(0, 200);
  const toggle = (id) => {
    if (busy) return;
    setBusy(true);
    save({ [settingKey('providers', provider.name, 'models')]: chosen.has(id)
      ? [...chosen].filter((m) => m !== id) : [...chosen, id] }).then(forgetModels).finally(() => setBusy(false));
  };
  return (
    <div className="rounded-lg border">
      <div className="relative border-b">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
        <label htmlFor={`pick-${provider.name}`} className="sr-only">{t('providers.search')}</label>
        <input id={`pick-${provider.name}`} value={query} onChange={(event) => setQuery(event.target.value)}
          placeholder={t('providers.search')}
          className="h-9 w-full rounded-t-lg bg-transparent pl-9 pr-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring" />
      </div>
      <ul className="scroll-thin max-h-56 overflow-y-auto py-1">
        {shown.map((m) => (
          <li key={m.id}>
            <label className="flex cursor-pointer items-center gap-2.5 px-3 py-1.5 text-sm hover:bg-accent">
              <input type="checkbox" checked={chosen.has(m.id)} disabled={busy} onChange={() => toggle(m.id)} className="accent-[hsl(var(--brand))]" />
              <span className="min-w-0 flex-1 truncate">{m.name}</span>
              <span className="truncate font-mono text-xs text-muted-foreground">{m.id}</span>
            </label>
          </li>
        ))}
        {shown.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">{t('providers.noModels')}</li>}
      </ul>
      <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">{t('providers.pickedCount', { count: chosen.size })}</p>
    </div>
  );
}

// Each offered model's window (reported and in use, with an override), effort steps and price.
function PerModel({ provider, table, save }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [models, setModels] = useState(null);
  const [query, setQuery] = useState('');
  const newest = useRef(0); // only the newest read counts, so an older one cannot overwrite it
  const load = useCallback(() => {
    const mine = ++newest.current;
    return loadModels(provider.name).then((listing) => mine === newest.current && setModels(listing.models.filter((m) => m.offered)))
      .catch(() => mine === newest.current && setModels([]));
  }, [provider.name]);
  useEffect(() => {
    load();
    const stop = onCatalogChange(load); // a default window, offer or key changed: the rows read again
    return () => { newest.current += 1; stop(); };
  }, [load]);
  if (!models) return <p className="text-sm text-muted-foreground" role="status">{t('common.loading')}</p>;
  const numbers = new Intl.NumberFormat(language);
  const price = new Intl.NumberFormat(language, { style: 'currency', currency: 'USD', maximumFractionDigits: 4 });
  const shown = models.filter((m) => `${m.id} ${m.name}`.toLowerCase().includes(query.toLowerCase())).slice(0, 100);
  const setWindow = async (id, value) => {
    await save({ [settingKey('providers', provider.name, 'windows', id)]: value });
    forgetModels();
    load();
  };
  return (
    <div className="space-y-3">
      <p className="text-xs leading-relaxed text-muted-foreground">{t('providers.windowTrust')}</p>
      <label htmlFor={`per-model-${provider.name}`} className="sr-only">{t('providers.search')}</label>
      <Input id={`per-model-${provider.name}`} value={query} onChange={(event) => setQuery(event.target.value)}
        placeholder={t('providers.search')} className="h-9" />
      <ul className="divide-y rounded-lg border">
        {shown.map((m) => {
          const own = table.windows?.[m.id];
          return (
            <li key={m.id} className="grid gap-2 px-3 py-3 text-sm sm:grid-cols-[minmax(0,1fr)_11rem]">
              <div className="min-w-0">
                <p className="truncate font-medium" title={m.id}>{m.name}</p>
                <p className="truncate font-mono text-xs text-muted-foreground">{m.id}</p>
                <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  <dt>{t('providers.reported')}</dt>
                  <dd>{m.window.reported ? numbers.format(m.window.reported) : t('providers.notReported')}</dd>
                  <dt>{t('providers.inUse')}</dt>
                  <dd className={cn(m.window.status !== 'ok' && 'text-warning')}>
                    {m.window.status === 'needed' ? t('providers.windowNeeded')
                      : m.window.status === 'too_small' ? t('providers.windowTooSmall', { window: numbers.format(m.window.in_use) })
                        : numbers.format(m.window.in_use)}
                  </dd>
                  <dt>{t('providers.effort')}</dt>
                  <dd>{m.effort.steps.length ? m.effort.steps.map((s) => t(`effort.${s}`)).join(' · ')
                    : t(m.effort.surface === 'unknown' ? 'effort.unknown' : 'effort.none')}</dd>
                  <dt>{t('providers.price')}</dt>
                  <dd>{m.pricing?.input != null && m.pricing?.output != null
                    ? t('providers.pricePerMillion', { input: price.format(m.pricing.input), output: price.format(m.pricing.output) })
                    : t('providers.priceUnknown')}</dd>
                </dl>
              </div>
              <Field label={t('providers.window')} htmlFor={`w-${provider.name}-${m.id}`}
                aside={<Restore disabled={own == null} onClick={() => setWindow(m.id, null)} />}>
                <CommitField id={`w-${provider.name}-${m.id}`} type="number" min={MIN_WINDOW} step={1} inputMode="numeric"
                  value={own} suggestions={WINDOW_PRESETS} allowEmpty placeholder={m.window.in_use ? String(m.window.in_use) : ''}
                  onCommit={(value, error) => (error ? null : setWindow(m.id, value))} />
              </Field>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Adds an OpenAI-compatible endpoint, such as a model server on this Mac: its name, its
// address and its key (an empty key is allowed for a local server that asks for none).
function AddEndpoint({ existing, onCancel, onAdded }) {
  const t = useT();
  const personal = useSettingsFile();
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [key, setKey] = useState('');
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  const nameOk = NAME.test(name) && !existing.includes(name);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const saved = await personal.save({ [settingKey('providers', name, 'kind')]: 'openai-compatible',
        [settingKey('providers', name, 'base_url')]: url.trim() });
      if (!saved) return;
      await put(`/api/keys/${encodeURIComponent(name)}`, { key });
      await onAdded();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4 rounded-xl border border-brand/30 bg-brand-soft/40 p-4">
      <h3 className="text-sm font-semibold">{t('providers.addTitle')}</h3>
      <p className="text-xs leading-relaxed text-muted-foreground">{t('providers.addHint')}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t('providers.name')} htmlFor="new-provider-name" hint={t('providers.nameHint')}>
          <Input id="new-provider-name" value={name} onChange={(event) => setName(event.target.value.toLowerCase())} className="h-9 font-mono" />
        </Field>
        <Field label={t('providers.address')} htmlFor="new-provider-url" hint={t('providers.addressHint')}>
          <Input id="new-provider-url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder={t('providers.addressPlaceholder')}
            className="h-9 font-mono" spellCheck={false} />
        </Field>
      </div>
      <Field label={t('providers.key')} htmlFor="new-provider-key" hint={t('providers.keyHint')}>
        <Input id="new-provider-key" type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)}
          className="h-9 font-mono" />
      </Field>
      {(problem || personal.problem) && <p role="alert" className="text-sm text-destructive">{errorText(t, problem ?? personal.problem)}</p>}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" size="sm" disabled={busy || !nameOk || !url.trim() || !key.trim()}>{t('providers.addSubmit')}</Button>
      </div>
    </form>
  );
}
