// i18n: migrated
// The model picker in the message box (the stage walk; slice-1 spec section 6.2, Models and
// effort): Auto first, then the researcher's last three models, and a search over the models
// the ready providers offer. A chosen model shows an effort slider with only that model's own
// steps, remembered per model in [models] efforts; a model not yet checked shows "Default",
// and one without reasoning control shows no slider. A model with no usable window is marked,
// and choosing it asks for its window first (section 8). With nothing chosen, the project's or
// the personal [models] default applies; choosing Auto overrides it.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Check, ChevronDown, Sparkles } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { ApiError, get, saveSettings } from '../api.js';
import { errorText, visible } from '../text.js';
import { WINDOW_PRESETS, choiceUpdates, decodeChoice, forgetModels, loadModels, onCatalogChange, settingKey } from '../settings.js';
import { CommitField } from './fields.jsx';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

const listeners = new Set();
const EFFORT_LEVELS = ['minimal', 'low', 'medium', 'high', 'xhigh']; // backend/budget_router.py
let choice = null; // read from the personal settings ([ui] model) once per window
let reading = null;

function publish(next) {
  choice = next;
  listeners.forEach((listener) => listener());
}

// Sets the choice and keeps it across launches in the personal settings (the window's origin
// changes at each launch, so the browser's storage would forget it).
export function setChoice(next) {
  publish(next);
  saveSettings(choiceUpdates(next)).catch(() => {});
}

// Reads the kept choice once per window, with its remembered effort (only a level a turn can
// ask for); a message, and the catalog's check of what is offered, wait for it. A failed read
// fails the message (the researcher is told) and is tried again the next time.
export function readChoice() {
  reading ??= get('/api/settings').then((settings) => {
    const kept = decodeChoice(settings.values?.ui?.model);
    const effort = kept?.model ? settings.values?.models?.efforts?.[kept.model] : null;
    if (kept && !choice) publish(kept.model ? { ...kept, effort: EFFORT_LEVELS.includes(effort) ? effort : null } : kept);
  }).catch((error) => {
    reading = null; // tried again next time; meanwhile nothing is sent on a guess
    throw error;
  });
  return reading;
}

export const currentChoice = () => choice;

// The model a message is sent with: { provider, model, effort }, { auto: true }, or null for the
// settings' default.
export function useModelChoice() {
  return useSyncExternalStore((listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }, () => choice);
}

// Every model the ready providers offer, with the recent ones, the remembered efforts and the
// personal default; read each time the picker opens, and once at first for a chosen model's
// effort steps. A chosen model no longer offered (its provider off, or no longer picked) is
// dropped, so it is never sent.
function useCatalog(open, projectId, chosenModel) {
  const [catalog, setCatalog] = useState(null);
  const latest = useRef(0); // the newest load; an older one that finishes later is dropped
  const here = useRef(true); // a picker that has gone starts no load and publishes none
  const load = useCallback(async () => {
    if (!here.current) return;
    const mine = ++latest.current;
    try {
      await readChoice(); // a kept choice is checked against the catalog like any other
      forgetModels({ quiet: true }); // the offers and windows as the settings hold them now
      const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
      const [{ providers }, { models: recent }, settings, project] = await Promise.all([
        get('/api/providers'), get('/api/models/recent'), get('/api/settings'),
        projectId ? get(`/api/settings${query}`) : Promise.resolve(null)]);
      const ready = providers.filter((p) => p.enabled && p.has_key);
      const listings = await Promise.allSettled(ready.map((p) => loadModels(p.name)));
      const models = listings.flatMap((listing, i) => (listing.status === 'fulfilled'
        ? listing.value.models.filter((m) => m.offered).map((m) => ({ ...m, provider: ready[i].name })) : []));
      if (mine !== latest.current || !here.current) return;
      const next = { models, recent, efforts: settings.values?.models?.efforts ?? {}, several: ready.length > 1,
        defaultModel: visible(project?.values?.models?.default) || visible(settings.values?.models?.default) || 'auto' };
      // A chosen model is dropped when its provider is no longer ready (the provider list says
      // so), or when its own provider's current listing no longer offers it.
      if (choice?.model) {
        const at = ready.findIndex((p) => p.name === choice.provider);
        const own = at >= 0 && listings[at].status === 'fulfilled' ? listings[at].value : null;
        const row = own?.models.find((m) => m.id === choice.model); // a row listed now is judged as it reads
        const gone = row ? !(row.offered && row.window.status === 'ok') : Boolean(own && !own.status?.error);
        if (at < 0 || gone) setChoice(null); // its provider gone, or it is no longer offered or usable
      }
      setCatalog(next);
    } catch {
      if (mine === latest.current && here.current) {
        setCatalog((current) => current ?? { models: [], recent: [], efforts: {}, several: false, defaultModel: 'auto' });
      }
    }
  }, [projectId]);
  useEffect(() => {
    here.current = true;
    load(); // at first, for the label and a chosen model's steps; then each time the picker opens
    return () => { // a load still under way when the picker goes is dropped, and none starts after
      here.current = false;
      latest.current += 1;
    };
  }, [load]);
  useEffect(() => {
    if (open) load();
  }, [open, load]);
  useEffect(() => { // a choice restored or made later is checked against the catalog too
    if (chosenModel) load();
  }, [chosenModel, load]);
  useEffect(() => onCatalogChange(() => load()), [load]); // a provider or its models changed in Settings
  return [catalog, setCatalog, load];
}

export function ModelPicker({ projectId }) {
  const t = useT();
  const chosen = useModelChoice();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [catalog, setCatalog, reload] = useCatalog(open, projectId, chosen?.model ? `${chosen.provider}:${chosen.model}` : null);
  const [fixing, setFixing] = useState(null); // a model whose window is asked for before it is chosen
  const [fixProblem, setFixProblem] = useState(null);
  const model = catalog?.models.find((m) => m.provider === chosen?.provider && m.id === chosen?.model);

  const results = useMemo(() => {
    if (!catalog) return [];
    const words = query.trim().toLowerCase();
    return catalog.models.filter((m) => !words || `${m.id} ${m.name} ${m.provider}`.toLowerCase().includes(words)).slice(0, 40);
  }, [catalog, query]);
  const recent = (catalog?.recent ?? []).map((r) => catalog.models.find((m) => m.provider === r.provider && m.id === r.model))
    .filter(Boolean);

  function pick(m) {
    if (!m) {
      setChoice({ auto: true });
    } else if (m.window.status === 'needed') { // a window can be set only where none is reported
      setFixing(m);
      return;
    } else if (m.window.status !== 'ok') {
      return; // reported below the smallest usable window: no setting can raise it
    } else {
      const remembered = catalog.efforts[m.id];
      setChoice({ provider: m.provider, model: m.id, name: m.name, effort: m.effort.steps.includes(remembered) ? remembered : null });
    }
    setOpen(false);
    setQuery('');
    setFixing(null);
    setFixProblem(null);
  }

  function setEffort(level) {
    setChoice({ ...chosen, effort: level });
    setCatalog((current) => current && { ...current, efforts: { ...current.efforts, [chosen.model]: level } });
    saveSettings({ [settingKey('models', 'efforts', chosen.model)]: level }).catch(() => {});
  }

  async function setWindow(m, value) {
    setFixProblem(null);
    try {
      if (await saveSettings({ [settingKey('providers', m.provider, 'windows', m.id)]: value }) === null) {
        setFixProblem('settings_changed'); // the file changed meanwhile: nothing was written
        return;
      }
    } catch (error) {
      setFixProblem(error instanceof ApiError ? error.code : 'internal');
      return; // the field stays, with the reason
    }
    setFixing(null);
    await reload();
  }

  const fallback = catalog?.defaultModel && catalog.defaultModel !== 'auto' ? catalog.defaultModel : null;
  const auto = chosen?.auto || (!chosen && !fallback);
  const label = chosen?.model ? chosen.name ?? chosen.model : auto ? t('picker.auto') : fallback;
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button type="button" aria-label={t('picker.label', { model: label })}
            className="flex min-w-0 max-w-56 items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
            {auto && <Sparkles className="size-3.5 shrink-0 text-brand" aria-hidden="true" />}
            <span className="truncate">{label}</span>
            <ChevronDown className="size-3.5 shrink-0" aria-hidden="true" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" side="top" className="w-80 p-0">
          <div className="border-b p-2">
            <label htmlFor="picker-search" className="sr-only">{t('picker.search')}</label>
            <input id="picker-search" autoFocus value={query} onChange={(event) => setQuery(event.target.value)}
              placeholder={t('picker.search')}
              className="h-8 w-full rounded-md bg-muted/60 px-2.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          </div>
          <div role="listbox" aria-label={t('picker.models')} className="scroll-thin max-h-80 overflow-y-auto p-1">
            {!query && (
              <Item selected={auto} onSelect={() => pick(null)} title={t('picker.auto')} detail={t('picker.autoHint')}
                icon={<Sparkles className="size-3.5 text-brand" aria-hidden="true" />} />
            )}
            {!query && recent.length > 0 && <Heading>{t('picker.recent')}</Heading>}
            {!query && recent.map((m) => <ModelItem key={`r:${m.provider}:${m.id}`} m={m} chosen={chosen} several={catalog.several} onPick={pick} />)}
            {catalog && (query || recent.length > 0) && <Heading>{query ? t('picker.results') : t('picker.all')}</Heading>}
            {!catalog && <p className="px-2 py-3 text-sm text-muted-foreground" role="status">{t('common.loading')}</p>}
            {catalog && results.map((m) => <ModelItem key={`${m.provider}:${m.id}`} m={m} chosen={chosen} several={catalog.several} onPick={pick} />)}
            {catalog && results.length === 0 && <p className="px-2 py-3 text-sm text-muted-foreground">{t('picker.none')}</p>}
          </div>
          {fixing && (
            <form className="space-y-1.5 border-t p-2" onSubmit={(event) => event.preventDefault()}>
              <label htmlFor="picker-window" className="block text-xs font-medium">{t('picker.setWindow', { model: fixing.name })}</label>
              <CommitField id="picker-window" type="number" min={4096} step={1} inputMode="numeric" suggestions={WINDOW_PRESETS}
                placeholder={t('providers.window')} onCommit={(value, error) => (error ? null : setWindow(fixing, value))} />
              {fixProblem && <p role="alert" className="text-xs text-destructive">{errorText(t, fixProblem)}</p>}
              <p className="text-[11px] leading-relaxed text-muted-foreground">{t('providers.windowTrust')}</p>
            </form>
          )}
        </PopoverContent>
      </Popover>
      {chosen?.model && model && <Effort model={model} level={chosen.effort} onChange={setEffort} />}
    </div>
  );
}

function Heading({ children }) {
  return <p className="px-2 pb-1 pt-2.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{children}</p>;
}

function Item({ selected, onSelect, title, detail, icon, disabled, badge }) {
  return (
    <button type="button" role="option" aria-selected={selected} disabled={disabled} onClick={onSelect}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent">
      <span className="grid size-4 shrink-0 place-items-center">{selected ? <Check className="size-3.5" aria-hidden="true" /> : icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate">{title}</span>
        {detail && <span className="block truncate text-xs text-muted-foreground">{detail}</span>}
      </span>
      {badge && <span className="shrink-0 rounded bg-warning/10 px-1.5 py-0.5 text-[11px] text-warning">{badge}</span>}
    </button>
  );
}

function ModelItem({ m, chosen, several, onPick }) {
  const t = useT();
  const usable = m.window.status === 'ok';
  return (
    <Item selected={chosen?.provider === m.provider && chosen?.model === m.id} onSelect={() => onPick(m)}
      disabled={m.window.status === 'too_small'}
      title={m.name} detail={several ? `${m.provider} · ${m.id}` : m.id}
      badge={usable ? null : t(m.window.status === 'needed' ? 'picker.windowNeeded' : 'picker.windowTooSmall')} />
  );
}

// The chosen model's own effort steps, lowest first, as a slider; "Default" until checked.
function Effort({ model, level, onChange }) {
  const t = useT();
  const steps = model.effort.steps;
  if (!steps.length) {
    return model.effort.surface === 'unknown'
      ? <span className="text-xs text-muted-foreground" title={t('effort.unknownHint')}>{t('effort.default')}</span> : null;
  }
  const index = steps.includes(level) ? steps.indexOf(level) : Math.floor((steps.length - 1) / 2); // unset: the middle
  return (
    <label className="flex items-center gap-2 text-xs text-muted-foreground">
      <span className="sr-only">{t('effort.label')}</span>
      <input type="range" min={0} max={steps.length - 1} step={1} value={index}
        aria-valuetext={level ? t(`effort.${level}`) : t('effort.default')}
        onChange={(event) => onChange(steps[Number(event.target.value)])} className="w-20 accent-[hsl(var(--brand))]" />
      <span className={cn('w-16 truncate', level && 'text-foreground')}>{level ? t(`effort.${level}`) : t('effort.default')}</span>
    </label>
  );
}
