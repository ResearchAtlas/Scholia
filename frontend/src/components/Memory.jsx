// i18n: migrated
// The memory views (S1-21a; slice-1 spec F10 Views and switch, S12): under Settings, then Advanced, personal memory and
// this project's apart, each record with its type, status, sources and since when it holds; Confirm, Edit (a new version
// that supersedes it, at the version shown) and Forget (its content deleted). Pending proposals are Things to confirm,
// kept or forgotten here too. Loaded with Settings.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError } from '../api.js';
import { useAction } from '../action.js';
import { visible } from '../text.js';
import { changeRecord, forgetRecord, memoryChanged, readMemory, recordText, review } from '../memory.js';
import { libraryEvents, readGuard } from '../library.js';
import { LoadState, Section } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';

export function MemorySection({ project }) {
  const t = useT();
  const [found, setFound] = useState(null);
  const [problem, setProblem] = useState(null);
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
      {!found ? <LoadState problem={problem} onRetry={load} /> : (
        <div className="grid gap-6">
          {found.memory_enabled === false && <p className="text-sm text-muted-foreground">{t('memory.offNote')}</p>}
          <Records title={t('memory.personal')} records={found.personal} onChanged={() => memoryChanged(project?.id ?? null)} />
          {project && <Records title={t('memory.project')} records={found.project} onChanged={() => memoryChanged(project.id)} />}
        </div>
      )}
    </Section>
  );
}

function Records({ title, records, onChanged }) {
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
          <ul className="grid gap-2">{pending.map((r) => <Proposal key={r.id} proposal={r} onChanged={onChanged} />)}</ul>
        </div>
      )}
      {kept.length > 0 && <ul className="divide-y rounded-lg border">{kept.map((r) => <Row key={r.id} record={r} onChanged={onChanged} />)}</ul>}
    </div>
  );
}

function Row({ record, onChanged }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const { busy, problem, run } = useAction();
  const [editing, setEditing] = useState(null);
  const written = record.type === 'progress' || record.type === 'agent_note'; // by the app or the agent: never confirmed
  async function act(action) {
    await run(action);
    onChanged();
  }
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
          if (visible(editing)) act(() => changeRecord(record, { content: editing })).then(() => setEditing(null));
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
      {problem && <p role="alert" className="text-xs text-destructive">{problem}</p>}
    </li>
  );
}

// One proposal in Things to confirm: Keep confirms it at the version shown (one whose record changed meanwhile comes
// back marked out of date); Edit saves the researcher's own words for it, confirmed; Forget deletes it.
export function Proposal({ proposal, onChanged }) {
  const t = useT();
  const { busy, problem, run } = useAction();
  const [editing, setEditing] = useState(null);
  async function act(body) {
    try {
      await run(() => review({ id: proposal.id, version: proposal.version, ...body }));
    } finally {
      onChanged?.();
    }
  }
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
      {problem && <p role="alert" className="text-xs text-destructive">{problem}</p>}
    </li>
  );
}
