// i18n: migrated
// The memory views (S1-21a; slice-1 spec F10 Views and switch, S12): under Settings, then Advanced, personal memory and
// this project's apart, each record with its type, status, sources and since when it holds; Confirm, Edit (a new version
// that supersedes it, at the version shown) and Forget (its content deleted). Pending proposals are Things to confirm,
// kept or forgotten here too. Loaded with Settings.
import { useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError } from '../api.js';
import { visible } from '../text.js';
import { attempt, changeRecord, draftShown, forgetRecord, keepUnsaved, keptEvents, keptUnsaved, memoryChanged, memoryState,
  readMemory, recordText, review, settled, withState } from '../memory.js';
import { libraryEvents, readGuard } from '../library.js';
import { LoadState, Section } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';

export function MemorySection({ project }) {
  const t = useT();
  const [found, setFound] = useState(null);
  const [problem, setProblem] = useState(null);
  const [unsaved, setUnsaved] = useUnsaved(`settings:${project?.id ?? ''}`); // what an action could not save (MB1, Q12)
  const guard = useRef(null); // which reads may be shown (library.js readGuard)
  if (guard.current === null) guard.current = readGuard();
  const load = useCallback(() => {
    const settle = guard.current.ask();
    return readMemory(project?.id).then((data) => { if (settle(true)) { setFound(data); setProblem(null); } },
      (error) => { if (settle(false)) setProblem(error instanceof ApiError ? error.code : 'internal'); });
  }, [project?.id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { // a paper deleted or memory changed anywhere: dropped at once, and read again
    const changed = () => { guard.current.libraryChanged(); setFound(null); load(); };
    libraryEvents.addEventListener('changed', changed); // memory changes too (memory.js memoryChanged)
    return () => libraryEvents.removeEventListener('changed', changed);
  }, [load]);
  return (
    <Section title={t('memory.title')} hint={t('memory.hint')}>
      <Unsaved unsaved={unsaved} read={found} records={found && [...found.personal, ...found.project]} onKeep={setUnsaved} />
      {!found ? <LoadState problem={problem} onRetry={load} /> : (
        <div className="grid gap-6">
          {found.memory_enabled === false && <p className="text-sm text-muted-foreground">{t('memory.offNote')}</p>}
          <Records title={t('memory.personal')} records={found.personal} onChanged={() => memoryChanged(project?.id ?? null)}
            onFailed={(failed, id) => setUnsaved((kept) => settled(kept, failed, id))} />
          {project && <Records title={t('memory.project')} records={found.project} onChanged={() => memoryChanged(project.id)}
            onFailed={(failed, id) => setUnsaved((kept) => settled(kept, failed, id))} />}
        </div>
      )}
    </Section>
  );
}

// What a view's actions could not save, kept by key (view and project) across remounts: [value, set] (Q12).
export function useUnsaved(key) {
  const value = useSyncExternalStore(subscribeKept, () => keptUnsaved(key), () => null);
  return [value, useCallback((next) => keepUnsaved(key, typeof next === 'function' ? next(keptUnsaved(key)) : next), [key])];
}
function subscribeKept(changed) {
  keptEvents.addEventListener('changed', changed);
  return () => keptEvents.removeEventListener('changed', changed);
}

// What a memory action could not save: the error, and the researcher's own text, until dismissed (MB1). onKeep(next)
// keeps what is shown: null once dismissed; the text let go once the server says its record is gone (Q15). read: the
// view's current read (null while it reads), records: what it lists; at every read the server is asked.
export function Unsaved({ unsaved, read, records, onKeep }) {
  const t = useT();
  const [state, setState] = useState(null); // the server's word on the kept text's record, for this read
  const id = unsaved?.draft ? unsaved.id : null;
  useEffect(() => {
    setState(null);
    if (id == null || !read) return undefined;
    let wanted = true;
    memoryState([id]).then((found) => { if (wanted) setState(found[id] ?? null); }, () => {});  // unanswered: not shown
    return () => { wanted = false; };
  }, [id, read]);
  useEffect(() => {
    const next = withState(unsaved, state);
    if (next !== unsaved) onKeep(next);
  }, [unsaved, state, onKeep]);
  if (!unsaved) return null;
  const draft = draftShown(unsaved, records, state);
  return (
    <div role="alert" className="grid gap-1 rounded-lg border border-destructive/40 px-3 py-2 text-xs">
      <p className="text-destructive">{unsaved.problem}</p>
      {draft && <p className="break-words text-muted-foreground">{t('memory.unsavedText', { text: draft })}</p>}
      <div><Button size="sm" variant="ghost" className="h-7" onClick={() => onKeep(null)}>{t('memory.dismiss')}</Button></div>
    </div>
  );
}

// An action from a row or a proposal: the views read again whatever happened (no client-held derived state); what it
// could not save goes to onFailed(failed, id), outside the rows the reread replaces, and so does its success
// (onFailed(null, id)), which clears a notice kept for the same record.
function useMemoryAction(onChanged, onFailed) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  async function act(action, draft = null, id = null) {
    setBusy(true);
    const failed = await attempt(t, action, draft, id);
    setBusy(false);
    onFailed?.(failed, id);
    onChanged?.();
    return !failed;
  }
  return { busy, act };
}

function Records({ title, records, onChanged, onFailed }) {
  const t = useT();
  const pending = records.filter((r) => r.status === 'proposed');
  const kept = records.filter((r) => r.status !== 'proposed');
  return (
    <div className="grid gap-2">
      <h3 className="text-sm font-medium">{title}</h3>
      {records.length === 0 && <p className="text-sm text-muted-foreground">{t('memory.none')}</p>}
      {pending.length > 0 && (
        <div className="grid gap-2">
          <p className="text-xs font-medium text-muted-foreground">{t('leftOff.confirm')}</p>
          <ul className="grid gap-2">{pending.map((r) => <Proposal key={r.id} proposal={r} onChanged={onChanged} onFailed={onFailed} />)}</ul>
        </div>
      )}
      {kept.length > 0 && <ul className="divide-y rounded-lg border">{kept.map((r) => <Row key={r.id} record={r} onChanged={onChanged}
        onFailed={onFailed} />)}</ul>}
    </div>
  );
}

function Row({ record, onChanged, onFailed }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const { busy, act } = useMemoryAction(onChanged, onFailed);
  const [editing, setEditing] = useState(null);
  const written = record.type === 'progress' || record.type === 'agent_note'; // by the app or the agent: never confirmed
  const since = record.valid_from ? new Intl.DateTimeFormat(language, { dateStyle: 'medium' }).format(new Date(record.valid_from)) : null;
  const kinds = [...new Set(record.sources.map((s) => s.kind))];
  return (
    <li className="grid gap-1.5 px-3 py-2.5 text-sm">
      <p className="break-words">{recordText(t, record)}</p>
      <p className="text-xs text-muted-foreground">{[t(`memory.type.${record.type}`), t(`memory.status.${record.status}`),
        t('memory.sources', { count: record.sources.length, kinds: kinds.map((k) => t(`memory.source.${k}`)).join(', ') }),
        since && t('memory.since', { date: since }), t('memory.version', { version: record.version })].filter(Boolean).join(' · ')}</p>
      {editing !== null ? (
        <form className="grid gap-2" onSubmit={(event) => {
          event.preventDefault();
          if (visible(editing)) act(() => changeRecord(record, { content: editing }), editing, record.id).then(() => setEditing(null));
        }}>
          <label className="sr-only" htmlFor={`memory-${record.id}`}>{t('memory.contentLabel')}</label>
          <Textarea id={`memory-${record.id}`} value={editing} onChange={(event) => setEditing(event.target.value)} rows={2} />
          <div className="flex gap-2">
            <Button type="submit" size="sm" className="h-7" disabled={busy || !visible(editing)}>{t('memory.save')}</Button>
            <Button type="button" size="sm" variant="ghost" className="h-7" onClick={() => setEditing(null)}>{t('memory.cancel')}</Button>
          </div>
        </form>
      ) : (
        <div className="flex flex-wrap gap-2">
          {!written && record.status === 'inferred' && (
            <Button size="sm" variant="outline" className="h-7" disabled={busy}
              onClick={() => act(() => changeRecord(record, { status: 'confirmed' }), null, record.id)}>{t('memory.confirm')}</Button>
          )}
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => setEditing(record.content)}>
            {t('memory.edit')}</Button>
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => act(() => forgetRecord(record), null, record.id)}>
            {t('memory.forget')}</Button>
        </div>
      )}
    </li>
  );
}

// One proposal in Things to confirm: Keep confirms it at the version shown (one whose record changed meanwhile comes
// back marked out of date); Edit saves the researcher's own words for it, confirmed; Forget deletes it.
export function Proposal({ proposal, onChanged, onFailed }) {
  const t = useT();
  const { busy, act: acted } = useMemoryAction(onChanged, onFailed);
  const [editing, setEditing] = useState(null);
  const act = (body) => acted(() => review({ id: proposal.id, version: proposal.version, ...body }), body.content ?? null,
    proposal.id);
  const marks = [proposal.structured?.older && t('memory.older'), proposal.structured?.out_of_date && t('memory.outOfDate'),
    proposal.structured?.source === 'answer' && t('memory.fromAnswer')].filter(Boolean);
  return (
    <li className="grid gap-1.5 rounded-lg border bg-background/70 px-3 py-2">
      <p className="break-words">{proposal.content}</p>
      {proposal.replaces && <p className="break-words text-xs text-muted-foreground">
        {t('memory.replaces', { content: proposal.replaces.content })}</p>}
      {marks.length > 0 && <p className="text-xs text-muted-foreground">{marks.join(' · ')}</p>}
      {editing !== null && (
        <form className="grid gap-2" onSubmit={(event) => { event.preventDefault(); if (visible(editing)) act({ action: 'edit', content: editing }); }}>
          <label className="sr-only" htmlFor={`proposal-${proposal.id}`}>{t('memory.contentLabel')}</label>
          <Textarea id={`proposal-${proposal.id}`} value={editing} onChange={(event) => setEditing(event.target.value)} rows={2} />
          <div className="flex gap-2">
            <Button type="submit" size="sm" className="h-7" disabled={busy || !visible(editing)}>{t('memory.save')}</Button>
            <Button type="button" size="sm" variant="ghost" className="h-7" onClick={() => setEditing(null)}>{t('memory.cancel')}</Button>
          </div>
        </form>
      )}
      {editing === null && (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" className="h-7 bg-brand text-brand-foreground hover:bg-brand/90" disabled={busy}
            onClick={() => act({ action: 'keep' })}>{t('memory.keep')}</Button>
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => setEditing(proposal.content)}>
            {t('memory.edit')}</Button>
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => act({ action: 'forget' })}>
            {t('memory.forget')}</Button>
        </div>
      )}
    </li>
  );
}
