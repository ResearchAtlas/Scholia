// i18n: migrated
// The Library (S7; slice-1 spec F3a): Add files and a drop zone, the confirmations its work waits
// on, and the project's papers, each Reading, Ready, or Needs attention with its reason, with its
// details under Details. A paper opens its own page (Paper.jsx). Search comes with S1-17.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { FilePlus2, FileText, TriangleAlert, Upload } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, post } from '../api.js';
import { useAction } from '../action.js';
import { errorText } from '../text.js';
import { fileSize } from '../backups.js';
import { ACCEPT, LOOKUP_OUTCOMES, addFiles, cancelledKey, authorNames, libraryEvents, newest, reasonKey, sortFiles, stateKey,
  typeKey, unsettled, yearOf } from '../library.js';
import { fraction } from '../runs.js';
import { Ask } from './Ask.jsx';
import { Paper } from './Paper.jsx';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const POLL_MS = 1500; // while a paper is read, a lookup runs or a question waits

// Reads the project's papers, again while anything is under way and whenever they change. Only the
// newest read's answer is kept: one asked for another project, or before a later read, is dropped.
export function useLibrary(projectId) {
  const [listing, setListing] = useState(null);
  const [problem, setProblem] = useState(null);
  const [reads, setReads] = useState(0);
  const [asked] = useState(newest);
  const load = useCallback(async () => {
    const current = asked();
    if (!projectId) return;
    try {
      const found = await get(`/api/projects/${encodeURIComponent(projectId)}/materials`);
      if (!current()) return;
      setListing(found);
      setProblem(null);
    } catch (error) {
      if (current()) setProblem(error instanceof ApiError ? error.code : 'internal');
    } finally {
      if (current()) setReads((n) => n + 1);
    }
  }, [projectId, asked]);
  useEffect(() => { setListing(null); load(); }, [load]);
  useEffect(() => {
    const changed = (event) => { if (event.detail === projectId) load(); };
    libraryEvents.addEventListener('changed', changed);
    return () => libraryEvents.removeEventListener('changed', changed);
  }, [projectId, load]);
  useEffect(() => {
    if (!unsettled(listing) && !problem) return undefined;
    const timer = setTimeout(load, POLL_MS);
    return () => clearTimeout(timer);
  }, [listing, problem, reads, load]);
  return { listing, problem, load };
}

// Adds files to the project, after leaving out those Scholia does not read: resolves to the
// answer, and says through notify what was left out or went wrong.
export async function addTo(projectId, fileList, t, notify, options) {
  const { kept, skipped } = sortFiles(fileList);
  if (skipped.length) notify(t('library.skipped', { count: skipped.length, names: skipped.join(', ') }));
  if (!kept.length) return null;
  try {
    const result = await addFiles(projectId, kept, options);
    if (result.problem) notify(errorText(t, result.problem)); // some were added before it failed
    return result;
  } catch (error) {
    notify(errorText(t, error instanceof ApiError ? error.code : 'internal'));
    return null;
  }
}

export function Library({ project }) {
  const t = useT();
  const { listing, problem, load } = useLibrary(project?.id);
  const [open, setOpen] = useState(null); // the paper whose page is shown
  const [notice, setNotice] = useState(null);
  const [adding, setAdding] = useState(false);
  const [over, setOver] = useState(false);
  const input = useRef(null);

  async function add(files) {
    setNotice(null);
    setAdding(true);
    const result = await addTo(project.id, files, t, setNotice);
    setAdding(false);
    if (result?.materials.some((m) => m.existing)) setNotice(t('library.alreadyHere'));
    load();
  }

  const paper = open && listing?.materials.find((m) => m.id === open);
  if (paper) {
    return <Paper material={paper} project={project} onBack={() => { setOpen(null); load(); }} onChanged={load} />;
  }
  const materials = listing?.materials ?? [];
  return (
    <div className={cn('flex h-full min-h-0 flex-col', over && 'bg-brand-soft/40')}
      onDragOver={(event) => { if (event.dataTransfer?.types?.includes('Files')) { event.preventDefault(); setOver(true); } }}
      onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setOver(false); }}
      onDrop={(event) => { event.preventDefault(); event.stopPropagation(); setOver(false); add(event.dataTransfer.files); }}>
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
        <p className="flex-1 text-xs text-muted-foreground" aria-live="polite">
          {listing && t('library.materialCount', { count: materials.length })}
        </p>
        <input ref={input} type="file" multiple accept={ACCEPT} className="hidden" aria-hidden="true" tabIndex={-1}
          data-testid="library-files" onChange={(event) => { add(event.target.files); event.target.value = ''; }} />
        <Button size="sm" variant="outline" className="h-8" disabled={adding || !project} onClick={() => input.current?.click()}>
          <FilePlus2 aria-hidden="true" />{t('library.addFiles')}
        </Button>
      </div>
      <div className="scroll-thin min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {problem && <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorText(t, problem)}</p>}
        {notice && <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">{notice}</p>}
        {adding && <p role="status" className="text-sm text-muted-foreground">{t('library.adding')}</p>}
        {(listing?.asks ?? []).map((ask) => (
          <Ask key={ask.ask_id} ask={ask} onAnswered={load} />
        ))}
        {listing === null && !problem && <p role="status" className="text-sm text-muted-foreground">{t('common.loading')}</p>}
        {listing && materials.length > 0 && (
          <ul className="divide-y rounded-lg border" aria-label={t('library.papers')}>
            {materials.map((material) => <PaperRow key={material.id} material={material} project={project}
              onOpen={() => setOpen(material.id)} onChanged={load} />)}
          </ul>
        )}
        {listing && (
          <button type="button" onClick={() => input.current?.click()} disabled={adding}
            className={cn('flex w-full flex-col items-center gap-2 rounded-xl border-2 border-dashed px-4 text-center text-sm text-muted-foreground transition-colors hover:border-brand/50 hover:text-foreground',
              materials.length ? 'py-5' : 'py-12', over && 'border-brand text-foreground')}>
            <Upload className="size-5" aria-hidden="true" />
            <span className="font-medium">{materials.length ? t('library.dropMore') : t('library.empty')}</span>
            <span className="text-xs">{t('library.formats')}</span>
          </button>
        )}
      </div>
    </div>
  );
}

// Reading a paper's file again, where its file has no reading by this version of Scholia and none is
// under way (material.readable): never read, read by an earlier version, or its reading failed.
export function ReadAgain({ material, onDone }) {
  const t = useT();
  const { busy, problem, run } = useAction();
  async function again() {
    if (await run(() => post(`/api/material-versions/${encodeURIComponent(material.version.id)}/read`))) onDone?.();
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={again}>{t('library.readAgain')}</Button>
      {problem && <span role="alert" className="text-xs text-destructive">{problem}</span>}
    </div>
  );
}

export function StateChip({ material }) {
  const t = useT();
  return (
    <span className={cn('inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-xs font-medium', {
      reading: 'bg-brand-soft text-brand', ready: 'bg-success/10 text-success', needs_attention: 'bg-warning/10 text-warning',
    }[material.state])}>{t(stateKey(material))}</span>
  );
}

export function Retracted({ material }) {
  const t = useT();
  if (material.retraction !== 'retracted') return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
      <TriangleAlert className="size-3" aria-hidden="true" />{t('library.retracted')}
    </span>
  );
}

export function Byline({ material }) {
  const t = useT();
  const names = authorNames(material.csl);
  const authors = names.length > 3 ? t('library.etAl', { names: names.slice(0, 3).join(', ') }) : names.join(', ');
  const parts = [authors, yearOf(material.csl), material.csl?.['container-title']].filter(Boolean);
  return parts.length ? <p className="truncate text-xs text-muted-foreground" title={parts.join(' · ')}>{parts.join(' · ')}</p> : null;
}

export function Progress({ material }) {
  const t = useT();
  const share = fraction(material.progress);
  if (material.state !== 'reading') return null;
  return (
    <div className="grid gap-1" role="progressbar" aria-label={t('library.readingLabel', { title: material.title })}
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={share === null ? undefined : Math.round(share * 100)}>
      <div className="h-1 overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full rounded-full bg-brand transition-[width] duration-200', share === null && 'w-1/3 animate-pulse')}
          style={share === null ? undefined : { width: `${Math.max(4, share * 100)}%` }} />
      </div>
    </div>
  );
}

function PaperRow({ material, project, onOpen, onChanged }) {
  const t = useT();
  const reason = reasonKey(material.reason);
  return (
    <li className="grid grid-cols-1 gap-1.5 px-3 py-3">
      <div className="flex min-w-0 items-start gap-2">
        <FileText className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <button type="button" onClick={onOpen} title={material.title}
          className="min-w-0 flex-1 truncate rounded-sm text-left text-sm font-medium hover:text-brand focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
          {material.title}
        </button>
        <StateChip material={material} />
      </div>
      <div className="grid min-w-0 grid-cols-1 gap-1.5 pl-6">
        <Byline material={material} />
        <Progress material={material} />
        {reason && <p className="text-xs text-warning">{t(reason, { count: material.extraction?.ocr_pages ?? 0 })}</p>}
        {material.readable && <ReadAgain material={material} onDone={onChanged} />}
        <div className="flex flex-wrap items-center gap-2"><Retracted material={material} /></div>
        <details className="group text-xs">
          <summary className="w-fit cursor-pointer select-none rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
            {t('library.details')}
          </summary>
          <Facts material={material} project={project} />
        </details>
      </div>
    </li>
  );
}

// What is known of a paper: its file, its reading, its details' source and its retraction check.
export function Facts({ material, project }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const date = (value) => new Intl.DateTimeFormat(language, { dateStyle: 'medium' }).format(new Date(value));
  const extraction = material.extraction;
  const rows = [
    material.version && [t('library.fact.file'), `${t(typeKey(material.version.media_type))} · ${fileSize(material.version.size, language)}`
      + (material.version.seq ? ` · ${t('library.version', { number: material.version.seq + 1 })}` : '')],
    extraction?.pages != null && [t('library.fact.pages'), String(extraction.pages)],
    extraction && [t('library.fact.passages'), String(extraction.passages)],
    extraction?.ocr_pages > 0 && [t('library.fact.ocr'), t('library.ocrWaiting', { count: extraction.ocr_pages })],
    [t('library.fact.details'), detailsSource(t, material, project, date)],
    material.lookup?.identifier && [t('library.fact.identifier'), material.lookup.identifier.replace(/^(doi|arxiv):/, '')],
    [t('library.fact.retraction'), material.retraction === 'retracted' ? t('library.retractedOn', { date: date(material.retraction_checked_at) })
      : material.retraction === 'none' ? t('library.notRetracted', { date: date(material.retraction_checked_at) })
        : t('library.retractionUnchecked')],
    [t('library.fact.added'), date(material.created_at)],
  ].filter(Boolean);
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-md bg-muted/50 px-3 py-2">
      {rows.map(([term, value]) => (
        <div key={term} className="contents">
          <dt className="text-muted-foreground">{term}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function detailsSource(t, material, project, date) {
  if (material.checked_by === 'researcher') return t('library.source.edited', { date: date(material.checked_at) });
  const lookup = material.lookup;
  if (material.checked_by === 'lookup') {
    return t('library.source.lookedUp', { source: t(`ask.service.${lookup?.source ?? 'openalex'}`), date: date(material.checked_at) });
  }
  if (!lookup) return t(project?.review_lock ? 'library.source.locked' : 'library.source.fromFile');
  if (lookup.waiting) return t('library.source.waiting');
  if (lookup.status === 'running') return t('library.source.lookingUp');
  if (lookup.status === 'cancelled') return t(cancelledKey(lookup));
  const outcome = lookup.outcome;
  return t(LOOKUP_OUTCOMES.includes(outcome) ? `library.source.${outcome}` : 'library.source.fromFile');
}
