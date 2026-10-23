// i18n: migrated
// The memory views (S1-21a; slice-1 spec F10 Views and switch, S12): under Settings, then Advanced, personal memory and
// this project's apart, each record with its type, status, sources and since when it holds; Confirm, Edit (a new version
// that supersedes it, at the version shown) and Forget (its content deleted). Pending proposals are Things to confirm,
// kept or forgotten here too. Loaded with Settings.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError } from '../api.js';
import { visible } from '../text.js';
import { attempt, changeRecord, forgetRecord, memoryChanged, readMemory, recordText, review } from '../memory.js';
import { libraryEvents, readGuard } from '../library.js';
import { LoadState, Section } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';

export function MemorySection({ project }) {
  const t = useT();
  const [found, setFound] = useState(null);
  const [problem, setProblem] = useState(null);
  const [unsaved, setUnsaved] = useState(null); // what an action could not save, kept across the reread (MB1)
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
      <Unsaved unsaved={unsaved} onDismiss={() => setUnsaved(null)} />
      {!found ? <LoadState problem={problem} onRetry={load} /> : (
        <div className="grid gap-6">
          {found.memory_enabled === false && <p className="text-sm text-muted-foreground">{t('memory.offNote')}</p>}
          <Records title={t('memory.personal')} records={found.personal} onChanged={() => memoryChanged(project?.id ?? null)}
            onFailed={setUnsaved} />
          {project && <Records title={t('memory.project')} records={found.project} onChanged={() => memoryChanged(project.id)}
            onFailed={setUnsaved} />}
        </div>
      )}
    </Section>
  );
}

// What a memory action could not save: the error, and the researcher's own text, until dismissed (MB1).
export function Unsaved({ unsaved, onDismiss }) {
  const t = useT();
  if (!unsaved) return null;
  return (
    <div role="alert" className="grid gap-1 rounded-lg border border-destructive/40 px-3 py-2 text-xs">
      <p className="text-destructive">{unsaved.problem}</p>
      {unsaved.draft && <p className="break-words text-muted-foreground">{t('memory.unsavedText', { text: unsaved.draft })}</p>}
      <div><Button size="sm" variant="ghost" className="h-7" onClick={onDismiss}>{t('memory.dismiss')}</Button></div>
    </div>
  );
}

// An action from a row or a proposal: the views read again whatever happened (no client-held derived state); what it
// could not save goes to onFailed, outside the rows the reread replaces.
function useMemoryAction(onChanged, onFailed) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  async function act(action, draft = null) {
    setBusy(true);
    const failed = await attempt(t, action, draft);
    setBusy(false);
    if (failed) onFailed?.(failed);
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
          if (visible(editing)) act(() => changeRecord(record, { content: editing }), editing).then(() => setEditing(null));
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
              onClick={() => act(() => changeRecord(record, { status: 'confirmed' }))}>{t('memory.confirm')}</Button>
          )}
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => setEditing(record.content)}>
            {t('memory.edit')}</Button>
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={() => act(() => forgetRecord(record))}>
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
  const act = (body) => acted(() => review({ id: proposal.id, version: proposal.version, ...body }), body.content ?? null);
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
