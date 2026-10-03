// i18n: migrated
// The model picker in the message box (the stage walk; slice-1 spec section 6.2, Models and
// effort): Auto first, then the researcher's last three models, and a search over the models
// the ready providers offer. A chosen model shows an effort slider with only that model's own
// steps, remembered per model in [models] efforts; a model not yet checked shows "Default",
// and one without reasoning control shows no slider. A model with no usable window is marked
// and cannot be chosen until its window is set in Settings (section 8).
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { Check, ChevronDown, Sparkles } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { get, saveSettings } from '../api.js';
import { loadModels, settingKey } from '../settings.js';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

const STORED = 'scholia.model';
const listeners = new Set();

function readChoice() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORED) ?? 'null');
    return stored && typeof stored.model === 'string' && typeof stored.provider === 'string' ? stored : null;
  } catch {
    return null; // ponytail: a private window forgets the choice; Auto is the default
  }
}

let choice = readChoice();

export function setChoice(next) {
  choice = next;
  try {
    localStorage.setItem(STORED, JSON.stringify(next));
  } catch {
    // remembered for this window only
  }
  listeners.forEach((listener) => listener());
}

// The model a message is sent with: { provider, model, effort } or null for Auto.
export function useModelChoice() {
  return useSyncExternalStore((listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }, () => choice);
}

// Every model the ready providers offer, with the recent ones and the remembered efforts; read
// when the picker opens, and when a model is already chosen, for its effort steps.
function useCatalog(active) {
  const [catalog, setCatalog] = useState(null);
  const load = useCallback(async () => {
    const [{ providers }, { models: recent }, settings] = await Promise.all([
      get('/api/providers'), get('/api/models/recent'), get('/api/settings')]);
    const ready = providers.filter((p) => p.enabled && p.has_key);
    const listings = await Promise.allSettled(ready.map((p) => loadModels(p.name)));
    const models = listings.flatMap((listing, i) => (listing.status === 'fulfilled'
      ? listing.value.models.filter((m) => m.offered).map((m) => ({ ...m, provider: ready[i].name })) : []));
    setCatalog({ models, recent, efforts: settings.values?.models?.efforts ?? {}, several: ready.length > 1 });
  }, []);
  useEffect(() => {
    if (active) load().catch(() => setCatalog({ models: [], recent: [], efforts: {}, several: false }));
  }, [active, load]);
  return catalog;
}

export function ModelPicker() {
  const t = useT();
  const chosen = useModelChoice();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const catalog = useCatalog(open || Boolean(chosen));
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
      setChoice(null);
    } else {
      const remembered = catalog.efforts[m.id];
      setChoice({ provider: m.provider, model: m.id, name: m.name, effort: m.effort.steps.includes(remembered) ? remembered : null });
    }
    setOpen(false);
    setQuery('');
  }

  function setEffort(level) {
    setChoice({ ...chosen, effort: level });
    saveSettings({ [settingKey('models', 'efforts', chosen.model)]: level }).catch(() => {});
  }

  const label = chosen ? chosen.name ?? chosen.model : t('picker.auto');
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button type="button" aria-label={t('picker.label', { model: label })}
            className="flex min-w-0 max-w-56 items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
            {!chosen && <Sparkles className="size-3.5 shrink-0 text-brand" aria-hidden="true" />}
            <span className="truncate">{label}</span>
            <ChevronDown className="size-3.5 shrink-0" aria-hidden="true" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" side="top" className="w-80 p-0">
          <div className="border-b p-2">
            <label htmlFor="picker-search" className="sr-only">{t('picker.search')}</label>
            <input id="picker-search" autoFocus value={query} onChange={(event) => setQuery(event.target.value)}
              placeholder={t('picker.search')} className="h-8 w-full rounded-md bg-muted/60 px-2.5 text-sm outline-none" />
          </div>
          <div role="listbox" aria-label={t('picker.models')} className="scroll-thin max-h-80 overflow-y-auto p-1">
            {!query && (
              <Item selected={!chosen} onSelect={() => pick(null)} title={t('picker.auto')} detail={t('picker.autoHint')}
                icon={<Sparkles className="size-3.5 text-brand" aria-hidden="true" />} />
            )}
            {!query && recent.length > 0 && <Heading>{t('picker.recent')}</Heading>}
            {!query && recent.map((m) => <ModelItem key={`r:${m.provider}:${m.id}`} m={m} chosen={chosen} several={catalog.several} onPick={pick} />)}
            {catalog && (query || recent.length > 0) && <Heading>{query ? t('picker.results') : t('picker.all')}</Heading>}
            {!catalog && <p className="px-2 py-3 text-sm text-muted-foreground" role="status">{t('common.loading')}</p>}
            {catalog && results.map((m) => <ModelItem key={`${m.provider}:${m.id}`} m={m} chosen={chosen} several={catalog.several} onPick={pick} />)}
            {catalog && results.length === 0 && <p className="px-2 py-3 text-sm text-muted-foreground">{t('picker.none')}</p>}
          </div>
        </PopoverContent>
      </Popover>
      {chosen && model && <Effort model={model} level={chosen.effort} onChange={setEffort} />}
      {chosen && !model && chosen.effort && <span className="text-xs text-muted-foreground">{t(`effort.${chosen.effort}`)}</span>}
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
    <Item selected={chosen?.provider === m.provider && chosen?.model === m.id} onSelect={() => onPick(m)} disabled={!usable}
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
