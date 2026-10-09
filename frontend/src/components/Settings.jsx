// i18n: migrated
// Settings (S10; slice-1 spec F12 and the stage walk): General, Providers and models,
// Subagents, This project and Advanced. Personal values live in the personal config.toml,
// the project's in its own; keys live in the credential store (section 4.4).
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, X } from 'lucide-react';
import { LANGUAGES, LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, patch, post } from '../api.js';
import { useAction } from '../action.js';
import { errorText, money } from '../text.js';
import { projectName } from '../projects.js';
import { BUDGET_SUGGESTIONS, loadModels, useInstructions, useSettingsFile, utf8Bytes, valueAt } from '../settings.js';
import { CommitField, Field, FileProblems, LoadState, Problem, Restore, Section, Segmented } from './fields.jsx';
import { AuditLog, PrivateAllowlist, ProjectProtection } from './Governance.jsx';
import { Providers } from './Providers.jsx';
import { BackupsSection } from './Backups.jsx';
import { ProjectExportSection } from './ProjectExport.jsx';
import { Ask } from './Ask.jsx';
import { fraction, retryRun, runOutcome } from '../runs.js';
import { LocalHelperSection, LocalOnlySearchNote } from './LocalHelper.jsx';
import { IndexSection } from './SearchIndex.jsx'; // S1-17
import { offerOutcome } from '../search.js'; // S1-17
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

const PAGES = ['general', 'providers', 'subagents', 'project', 'advanced'];
const WORKFLOWS = { title: 'settings.workflowTitle', extract: 'settings.workflowExtract', lookup: 'settings.workflowLookup',
  full_backup: 'settings.workflowFullBackup', project_export: 'settings.workflowExport',
  index: 'settings.workflowIndex', model_offer: 'settings.workflowModelOffer' }; // the last two: S1-17
const POLL_MS = 2000; // ponytail: polled while open; a pushed event stream if the list grows
const EFFORT_SUGGESTIONS = ['minimal', 'low', 'medium', 'high', 'xhigh'];
const LIMITS = ['agent_steps', 'tool_calls', 'turn_minutes'];
const ROLES = ['council', 'chairman', 'router', 'judge', 'extractor'];
const MAX_SUBAGENT_MODELS = 5;

export function Settings({ open, onOpenChange, health, project, onLanguage, onProjectChanged }) {
  const t = useT();
  const [page, setPage] = useState('general');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[min(88vh,820px)] max-w-4xl flex-col gap-0 p-0 sm:flex-row">
        <DialogTitle className="sr-only">{t('settings.title')}</DialogTitle>
        <DialogDescription className="sr-only">{t('settings.description')}</DialogDescription>
        <nav aria-label={t('settings.title')}
          className="flex shrink-0 gap-1 overflow-x-auto border-b p-2 sm:w-52 sm:flex-col sm:border-b-0 sm:border-r sm:bg-sidebar sm:p-3">
          <p className="hidden px-2 pb-2 pt-1 text-sm font-semibold sm:block">{t('settings.title')}</p>
          {PAGES.map((name) => (
            <button key={name} type="button" onClick={() => setPage(name)} aria-current={page === name ? 'page' : undefined}
              className={cn('whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-sm transition-colors hover:bg-accent',
                page === name && 'bg-accent font-medium')}>
              {t(`settings.page.${name}`)}
            </button>
          ))}
        </nav>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-6 py-6 sm:px-8">
          {open && page === 'general' && <General onLanguage={onLanguage} projectId={project?.id} />}
          {open && page === 'providers' && <Providers />}
          {open && page === 'subagents' && <Subagents />}
          {open && page === 'project' && project && <ThisProject project={project} onProjectChanged={onProjectChanged}
            onOpenAdvanced={() => setPage('advanced')} />}
          {open && page === 'advanced' && <Advanced health={health} project={project} />}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PageTitle({ children }) {
  return <h2 className="text-lg font-semibold tracking-tight">{children}</h2>;
}

function General({ onLanguage, projectId }) {
  const t = useT();
  const personal = useSettingsFile();
  const [problem, setProblem] = useState(null);
  const values = personal.values;
  if (!values) return <LoadState problem={personal.problem} onRetry={personal.reload} />;
  return (
    <div className="space-y-8">
      <PageTitle>{t('settings.page.general')}</PageTitle>
      <Problem code={personal.problem ?? problem} />
      <FileProblems problems={personal.problems} />
      <Field label={t('settings.language')}>
        <div>
          <Segmented label={t('settings.language')} value={values.ui.language}
            options={[{ value: 'system', label: t('settings.languageSystem') },
              ...LANGUAGES.map((code) => ({ value: code, label: t(`language.${code}`) }))]}
            onChange={async (value) => { if (await personal.save({ 'ui.language': value })) onLanguage(value); }} />
        </div>
      </Field>
      <Field label={t('settings.followUp')} hint={t('settings.followUpHint')}>
        <div>
          <Segmented label={t('settings.followUp')} value={values.ui.follow_up}
            options={[{ value: 'steer', label: t('settings.followUpSteer') }, { value: 'queue', label: t('settings.followUpQueue') }]}
            onChange={(value) => personal.save({ 'ui.follow_up': value })} />
        </div>
      </Field>
      <Field label={t('settings.conversationBudget')} htmlFor="conversation-budget" hint={t('settings.conversationBudgetHint')}>
        <div className="max-w-40">
          <CommitField id="conversation-budget" type="number" above={0} value={values.budget.conversation_usd}
            suggestions={BUDGET_SUGGESTIONS}
            onCommit={checked(setProblem, (value) => personal.save({ 'budget.conversation_usd': value }))} />
        </div>
      </Field>
      <InstructionsEditor label={t('settings.personalInstructions')} hint={t('settings.personalInstructionsHint')}
        withProject={projectId} />
    </div>
  );
}

// An AGENTS.md editor. The combined personal and project instructions are capped at 32 KiB;
// the editor warns before that, as the text is typed.
function InstructionsEditor({ projectId, withProject, label, hint }) {
  const t = useT();
  const instructions = useInstructions(projectId, withProject);
  const rejected = useRef(null); // a draft whose save met a changed file: kept to reconcile
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false); // the text cannot change while it is saved and read back
  const [confirming, setConfirming] = useState(false); // a file not in UTF-8 is rewritten only when confirmed
  const file = instructions.file;
  useEffect(() => {
    setDraft(rejected.current ?? file?.text ?? null);
    rejected.current = null;
    setConfirming(false);
  }, [file?.text, file?.hash]);
  if (!file || draft === null) return <LoadState problem={instructions.problem} onRetry={instructions.reload} />;
  // As the backend joins them: the two files, with a blank line between when both hold text.
  const own = utf8Bytes(draft);
  const combined = own + file.other_bytes + (own && file.other_bytes ? 2 : 0);
  const over = combined > file.cap_bytes;
  const id = projectId ? 'project-instructions' : 'personal-instructions';
  return (
    <div className="space-y-2">
      <Field label={label} htmlFor={id} hint={hint}
        aside={<span className={cn('text-xs tabular-nums', over ? 'text-destructive' : 'text-muted-foreground')}>
          {t('settings.instructionsSize', { used: Math.ceil(combined / 1024), cap: file.cap_bytes / 1024 })}
        </span>}>
        <Textarea id={id} value={draft} onChange={(event) => setDraft(event.target.value)} rows={8} readOnly={saving || file.unreadable}
          className="font-mono text-[13px] max-md:leading-relaxed" spellCheck={false} />
      </Field>
      {file.unreadable && <p role="alert" className="text-xs text-destructive">{t('settings.instructionsUnreadable')}</p>}
      {file.replaced && <p role="alert" className="text-xs text-warning">{t('settings.instructionsReplaced')}</p>}
      {over && <p role="alert" className="text-xs text-destructive">{t('settings.instructionsOverCap')}</p>}
      <Problem code={instructions.problem} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" disabled={saving || draft === file.text}
          onClick={() => { setDraft(file.text); setConfirming(false); }}>{t('common.cancel')}</Button>
        <Button size="sm" disabled={saving || file.unreadable || draft === file.text} onClick={async () => {
          if (file.replaced && !confirming) {
            setConfirming(true);
            return;
          }
          setSaving(true);
          await instructions.save(draft, () => { rejected.current = draft; }); // on a changed file the draft stays
          setSaving(false);
        }}>{confirming ? t('settings.instructionsReplaceConfirm') : t('common.save')}</Button>
      </div>
    </div>
  );
}

// The models offered by the providers that are ready, for suggestions: [{ provider, id, name }].
function useOfferedModels() {
  const [models, setModels] = useState([]);
  useEffect(() => {
    let live = true;
    get('/api/providers').then(async ({ providers }) => {
      const ready = providers.filter((p) => p.enabled && p.has_key);
      const listings = await Promise.allSettled(ready.map((p) => loadModels(p.name)));
      const found = listings.flatMap((listing, i) => (listing.status === 'fulfilled'
        ? listing.value.models.filter((m) => m.offered).map((m) => ({ provider: ready[i].name, id: m.id, name: m.name }))
        : []));
      if (live) setModels(found);
    }).catch(() => {});
    return () => { live = false; };
  }, []);
  return models;
}

function Subagents() {
  const t = useT();
  const personal = useSettingsFile();
  const offered = useOfferedModels();
  const [busy, setBusy] = useState(false); // one change at a time: the next starts from the saved list
  const [adding, setAdding] = useState('');
  const [problem, setProblem] = useState(null);
  const values = personal.values;
  if (!values) return <LoadState problem={personal.problem} onRetry={personal.reload} />;
  const list = [...new Set(values.subagents.models ?? [])]; // a ranked list names each model once
  // One change at a time, each from the list as saved (or read again after a refusal), so a
  // change never builds on one that was not written.
  const change = (edit) => {
    if (busy) return;
    const next = edit(list);
    if (!next) return;
    setBusy(true);
    personal.save({ 'subagents.models': next }).finally(() => setBusy(false));
  };
  const move = (id, by) => change((base) => { // by the model, in the list the last change left
    const from = base.indexOf(id);
    const to = from + by;
    if (from < 0 || to < 0 || to >= base.length) return null;
    const next = [...base];
    next.splice(to, 0, next.splice(from, 1)[0]);
    return next;
  });
  const remove = (id) => change((base) => base.filter((m) => m !== id));
  function add() {
    const id = adding.trim();
    change((base) => (!id || base.includes(id) || base.length >= MAX_SUBAGENT_MODELS ? null : [...base, id]));
    setAdding('');
  }
  return (
    <div className="space-y-8">
      <PageTitle>{t('settings.page.subagents')}</PageTitle>
      <Problem code={personal.problem ?? problem} />
      <FileProblems problems={personal.problems} />
      <Section title={t('subagents.models')} hint={t('subagents.modelsHint')}>
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">{t('subagents.count', { count: list.length, max: MAX_SUBAGENT_MODELS })}</span>
        </div>
        {list.length === 0 && <p className="text-sm text-muted-foreground">{t('subagents.empty')}</p>}
        <ol className="divide-y rounded-lg border">
          {list.map((id, i) => (
            <li key={id} className="flex items-center gap-2 px-3 py-2 text-sm">
              <span className="w-5 text-xs tabular-nums text-muted-foreground">{i + 1}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[13px]" title={id}>{id}</span>
              <Button variant="ghost" size="icon" className="size-7" disabled={busy || i === 0} onClick={() => move(id, -1)}
                aria-label={t('subagents.up', { model: id })}><ArrowUp aria-hidden="true" /></Button>
              <Button variant="ghost" size="icon" className="size-7" disabled={busy || i === list.length - 1} onClick={() => move(id, 1)}
                aria-label={t('subagents.down', { model: id })}><ArrowDown aria-hidden="true" /></Button>
              <Button variant="ghost" size="icon" className="size-7" disabled={busy} onClick={() => remove(id)}
                aria-label={t('subagents.remove', { model: id })}><X aria-hidden="true" /></Button>
            </li>
          ))}
        </ol>
        {list.length < MAX_SUBAGENT_MODELS && (
          <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); add(); }}>
            <label htmlFor="subagent-add" className="sr-only">{t('subagents.add')}</label>
            <input id="subagent-add" list="subagent-models" value={adding} onChange={(event) => setAdding(event.target.value)}
              placeholder={t('subagents.addPlaceholder')}
              className="h-9 flex-1 rounded-md border border-input bg-transparent px-3 font-mono text-[13px] outline-hidden focus-visible:ring-1 focus-visible:ring-ring" />
            <datalist id="subagent-models">
              {offered.map((m) => <option key={`${m.provider}:${m.id}`} value={m.id}>{m.name}</option>)}
            </datalist>
            <Button type="submit" variant="outline" size="sm" className="h-9" disabled={busy || !adding.trim()}>{t('subagents.add')}</Button>
          </form>
        )}
      </Section>
      <Section title={t('settings.advanced')}>
        <div className="grid gap-5 sm:grid-cols-3">
          <Field label={t('subagents.atOnce')} htmlFor="subagents-at-once">
            <CommitField id="subagents-at-once" type="number" min={1} step={1} value={values.subagents.at_once}
              onCommit={checked(setProblem, (value) => personal.save({ 'subagents.at_once': value }))} />
          </Field>
          <Field label={t('subagents.toolCalls')} htmlFor="subagents-tool-calls">
            <CommitField id="subagents-tool-calls" type="number" min={1} step={1} value={values.subagents.tool_calls}
              onCommit={checked(setProblem, (value) => personal.save({ 'subagents.tool_calls': value }))} />
          </Field>
          <Field label={t('subagents.effortCap')} htmlFor="subagents-effort-cap" hint={t('subagents.effortCapHint')}>
            <CommitField id="subagents-effort-cap" value={values.subagents.effort_cap} allowEmpty
              suggestions={EFFORT_SUGGESTIONS} placeholder={t('subagents.noCap')}
              onCommit={(value) => personal.save({ 'subagents.effort_cap': value })} />
          </Field>
        </div>
      </Section>
    </div>
  );
}

function ThisProject({ project, onProjectChanged, onOpenAdvanced }) {
  const t = useT();
  const own = useSettingsFile(project.id);
  const personal = useSettingsFile();
  const [problem, setProblem] = useState(null);
  const general = project.kind === 'general';

  async function change(fields) {
    setProblem(null);
    try {
      await patch(`/api/projects/${project.id}`, fields);
      onProjectChanged();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
    }
  }

  if (!own.values || !personal.values) {
    return <LoadState problem={own.problem ?? personal.problem} onRetry={() => { own.reload(); personal.reload(); }} />;
  }
  const values = own.values;
  return (
    <div className="space-y-8">
      <PageTitle>{t('settings.projectTitle', { name: projectName(t, project) })}</PageTitle>
      <Problem code={own.problem ?? problem} />
      <FileProblems problems={own.problems} />
      {!general && (
        <Field label={t('project.nameLabel')} htmlFor="project-name">
          <CommitField id="project-name" value={project.name} onCommit={(name) => change({ name })} />
        </Field>
      )}
      {general ? <p className="text-sm text-muted-foreground">{t('protection.generalNote')}</p>
        : <ProjectProtection project={project} onProjectChanged={onProjectChanged} />}
      {project.sensitivity === 'local_only' && <LocalOnlySearchNote onOpenAdvanced={onOpenAdvanced} />}
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label={t('project.venue')} htmlFor="project-venue" hint={t('project.venueHint')}>
          <CommitField id="project-venue" value={project.target_venue} allowEmpty placeholder={t('project.venuePlaceholder')}
            onCommit={(target_venue) => change({ target_venue })} />
        </Field>
        <Field label={t('project.citationStyle')} htmlFor="project-citation-style" hint={t('project.citationStyleHint')}>
          <CommitField id="project-citation-style" value={values.project.citation_style} allowEmpty
            placeholder={t('project.citationStyleAuto')} onCommit={(value) => own.save({ 'project.citation_style': value })} />
        </Field>
      </div>
      <Field label={t('project.budget')} htmlFor="project-budget" hint={t('project.budgetHint')}>
        <div className="max-w-40">
          <CommitField id="project-budget" type="number" above={0} value={values.project.budget_usd}
            suggestions={[25, 50, 100, 200]}
            onCommit={checked(setProblem, (value) => own.save({ 'project.budget_usd': value }))} />
        </div>
      </Field>
      <InstructionsEditor projectId={project.id} label={t('settings.projectInstructions')} hint={t('settings.projectInstructionsHint')} />
      <Section title={t('settings.projectLimits')} hint={t('settings.projectLimitsHint')}>
        <LimitFields file={own} inherited={personal.values.limits} onProblem={setProblem} prefix="project-limit" />
      </Section>
      <Section title={t('search.indexTitle')} hint={t('search.indexHint')}>
        <IndexSection project={project} />
      </Section>
      <Section title={t('export.title')} hint={t('export.hint')}>
        <ProjectExportSection project={project} />
      </Section>
    </div>
  );
}

// The turn limits (slice-1 spec section 13). In a project file each one overrides the
// personal value, which an empty field inherits.
// A checked field's commit: an invalid entry says so; a valid one clears that and is saved.
function checked(setProblem, save) {
  return (value, error) => {
    setProblem(error ?? null);
    if (!error) save(value);
  };
}

function LimitFields({ file, inherited, onProblem, prefix }) {
  const t = useT();
  return (
    <div className="grid gap-5 sm:grid-cols-3">
      {LIMITS.map((name) => {
        const value = valueAt(file.values, `limits.${name}`);
        return (
          <Field key={name} label={t(`limits.${name}`)} htmlFor={`${prefix}-${name}`}
            hint={inherited ? t(`settings.from.${value == null ? 'personal' : 'project'}`) : undefined}>
            <CommitField id={`${prefix}-${name}`} type="number" min={1} step={1} value={value} allowEmpty={Boolean(inherited)}
              placeholder={inherited ? String(inherited[name]) : undefined}
              onCommit={checked(onProblem, (next) => file.save({ [`limits.${name}`]: next }))} />
          </Field>
        );
      })}
    </div>
  );
}

function Advanced({ health, project }) {
  const t = useT();
  const personal = useSettingsFile();
  const offered = useOfferedModels();
  const [problem, setProblem] = useState(null);
  const ids = useMemo(() => [...new Set(offered.map((m) => m.id))], [offered]);
  if (!personal.values) return <LoadState problem={personal.problem} onRetry={personal.reload} />;
  return (
    <div className="space-y-10">
      <PageTitle>{t('settings.page.advanced')}</PageTitle>
      <Problem code={personal.problem ?? problem} />
      <FileProblems problems={personal.problems} />
      <Section title={t('settings.roles')} hint={t('settings.rolesHint')}>
        <div className="grid gap-5 sm:grid-cols-2">
          {ROLES.map((role) => {
            const value = personal.values.models[role];
            return (
              <Field key={role} label={t(`roles.${role}`)} htmlFor={`role-${role}`} hint={t(`roles.${role}Hint`)}
                aside={<Restore disabled={value == null} onClick={() => personal.save({ [`models.${role}`]: null })} />}>
                <CommitField id={`role-${role}`} value={Array.isArray(value) ? value.join(', ') : value} allowEmpty
                  suggestions={ids} placeholder={t('roles.automatic')} className="font-mono text-[13px]"
                  onCommit={(next) => personal.save({ [`models.${role}`]: role === 'council' && next?.includes(',')
                    ? next.split(',').map((s) => s.trim()).filter(Boolean) : next })} />
              </Field>
            );
          })}
        </div>
      </Section>
      <Section title={t('settings.limits')} hint={t('settings.limitsHint')}>
        <LimitFields file={personal} onProblem={setProblem} prefix="limit" />
      </Section>
      <Section title={t('settings.localServers')} hint={t('settings.localServersHint')} />
      <LocalHelperSection project={project} />
      <PrivateAllowlist />
      <AuditLog project={project} />
      <BackgroundRuns />
      <Section title={t('settings.backups')}>
        <BackupsSection />
      </Section>
      <Section title={t('settings.dataFolder')} hint={t('settings.dataFolderHint')}>
        <p className="break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">{health.data_folder}</p>
      </Section>
    </div>
  );
}

function BackgroundRuns() {
  const t = useT();
  const language = useContext(LanguageContext);
  const [runs, setRuns] = useState(null);
  const [problem, setProblem] = useState(null); // a cancel that failed; the next cancel clears it
  const [polled, setPolled] = useState(null); // the last read's error; a read that succeeds clears it

  const load = useCallback(() => get('/api/activity').then((data) => {
    setRuns(data.runs);
    setPolled(null);
  }).catch((error) => {
    setPolled(error instanceof ApiError ? error.code : 'internal');
  }), []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function act(action) {
    setProblem(null);
    try {
      await action();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal');
    }
    load();
  }

  const dates = new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' });
  return (
    <Section title={t('settings.backgroundRuns')} hint={t('settings.backgroundRunsHint')}>
      <Problem code={problem ?? polled} />
      {runs?.length === 0 && <p className="text-sm text-muted-foreground">{t('settings.noRuns')}</p>}
      {runs?.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {runs.map((run) => <RunRow key={run.run_id} run={run} dates={dates} onChanged={load}
            onCancel={() => act(() => post(`/api/runs/${run.run_id}/cancel`))} onRetry={() => act(() => retryRun(run.run_id))} />)}
        </ul>
      )}
    </Section>
  );
}

// One background run: what it does and for whom, its status and progress, why it stopped, the
// question it waits on, Cancel while it runs and Retry where a new run can do it again.
function RunRow({ run, dates, onCancel, onRetry, onChanged }) {
  const t = useT();
  const { busy, run: press } = useAction(); // Cancel and Retry, each disabled while either is pending
  const language = useContext(LanguageContext);
  const share = fraction(run.progress);
  const outcome = run.status === 'running' ? null : runOutcome(run);
  const project = projectName(t, { kind: run.project_kind, name: run.project_name });
  const papers = run.materials?.titles?.length
    ? (run.materials.count > run.materials.titles.length
      ? t('runs.papersMore', { titles: run.materials.titles.join(', '), count: run.materials.count - run.materials.titles.length })
      : run.materials.titles.join(', '))
    : null;
  return (
    <li className="grid grid-cols-1 gap-2 px-3 py-2.5 text-sm">
      <div className="flex min-w-0 items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{WORKFLOWS[run.workflow] ? t(WORKFLOWS[run.workflow]) : run.workflow}</p>
          {papers && <p className="truncate text-xs" title={papers}>{papers}</p>}
          <p className="truncate text-xs text-muted-foreground">
            {project} · {t('runs.started', { date: dates.format(new Date(run.started_at)) })}
          </p>
        </div>
        {run.cost_usd > 0 && <span className="text-xs tabular-nums text-muted-foreground">{t('common.cost', { cost: money(run.cost_usd, language) })}</span>}
        <span className={cn('rounded-full px-2 py-0.5 text-xs', {
          running: 'bg-brand-soft text-brand', succeeded: 'bg-success/10 text-success', failed: 'bg-destructive/10 text-destructive',
        }[run.status] ?? 'bg-muted text-muted-foreground')}>{t(`status.${run.status}`)}</span>
        {run.status === 'running' && (
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => press(onCancel)}>{t('settings.cancelRun')}</Button>
        )}
        {run.retryable && <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => press(onRetry)}>{t('runs.retry')}</Button>}
      </div>
      {run.status === 'running' && share !== null && (
        <div className="flex items-center gap-2" role="progressbar" aria-label={t('runs.progressLabel')}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(share * 100)}>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-brand transition-[width] duration-200" style={{ width: `${Math.max(3, share * 100)}%` }} />
          </div>
          <span className="text-xs tabular-nums text-muted-foreground">{t('runs.progress', { done: run.progress.done, total: run.progress.total })}</span>
        </div>
      )}
      {outcome && !outcome.ok && (
        <p className="text-xs text-muted-foreground">{outcome.code ? errorText(t, outcome.code) : t(outcome.key)}</p>
      )}
      {run.workflow === 'model_offer' && run.status === 'succeeded' && offerOutcome(t, run.result) && (
        <p className="text-xs text-muted-foreground">{offerOutcome(t, run.result)}</p>
      )}
      {run.ask && <Ask ask={run.ask} onAnswered={onChanged} />}
    </li>
  );
}
