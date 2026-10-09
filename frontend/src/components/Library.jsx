// i18n: migrated
// The Library (S7; slice-1 spec F3a): search, Add files and a drop zone, the confirmations its work
// waits on, and the project's papers, each Reading, Ready, or Needs attention with its reason, with
// its details under Details. A paper opens its own page (Paper.jsx); a search result opens it at its
// passage.
import { useCallback, useContext, useEffect, useId, useRef, useState, useSyncExternalStore } from 'react';
import { FilePlus2, FileText, Search, TriangleAlert, Upload, X } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, post } from '../api.js';
import { useAction } from '../action.js';
import { errorText } from '../text.js';
import { fileSize } from '../backups.js';
import { ACCEPT, addFiles, authorNames, detailsSource, latestLookup, libraryEvents, newest, reasonKey, sortFiles, stateKey,
  typeKey, unsettled, uploadsWaiting, watchUploads, yearOf } from '../library.js';
import { fraction } from '../runs.js';
import { Ask } from './Ask.jsx';
import { Paper } from './Paper.jsx';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
// S1-17: search and the search index
import { Input } from '@/components/ui/input';
import { downloadOffered } from '../helper.js';
import { fieldKey, indexBusy, indexStatus, paperIndexed, queryOf, searchNote, searchProject, whereIs } from '../search.js';
import { LocalOnlySearchNote } from './LocalHelper.jsx';

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
  const [open, setOpen] = useState(null); // the paper whose page is shown: { id, target } (a result's passage)
  const index = useIndex(project?.id, listing);
  const search = useSearch(project?.id);
  const [notice, setNotice] = useState(null);
  const adding = useSyncExternalStore(watchUploads, uploadsWaiting) > 0; // while any upload is still to finish
  const [over, setOver] = useState(false);
  const input = useRef(null);

  async function add(files) {
    setNotice(null);
    const result = await addTo(project.id, files, t, setNotice);
    if (result?.materials.some((m) => m.existing)) setNotice(t('library.alreadyHere'));
    load();
  }

  const paper = open && listing?.materials.find((m) => m.id === open.id);
  if (paper) {
    return <Paper material={paper} project={project} index={index} target={open.target}
      onBack={() => { setOpen(null); load(); }} onChanged={load} />;
  }
  const searching = Boolean(search.shown);
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
      {project && <SearchField search={search} />}
      <div className="scroll-thin min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {project?.sensitivity === 'local_only' && <LocalOnlySearchNote />}
        <SearchResults search={search} project={project}
          onOpen={(result) => setOpen({ id: result.material_id, target: { id: result.passage_id, ordinal: result.ordinal } })} />
        {problem && <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorText(t, problem)}</p>}
        {notice && <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">{notice}</p>}
        {adding && <p role="status" className="text-sm text-muted-foreground">{t('library.adding')}</p>}
        {(listing?.asks ?? []).map((ask) => (
          <Ask key={ask.ask_id} ask={ask} onAnswered={load} />
        ))}
        {listing === null && !problem && <p role="status" className="text-sm text-muted-foreground">{t('common.loading')}</p>}
        {listing && materials.length > 0 && !searching && (
          <ul className="divide-y rounded-lg border" aria-label={t('library.papers')}>
            {materials.map((material) => <PaperRow key={material.id} material={material} project={project} index={index}
              onOpen={() => setOpen({ id: material.id })} onChanged={load} />)}
          </ul>
        )}
        {listing && !searching && (
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

function PaperRow({ material, project, index, onOpen, onChanged }) {
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
          <Facts material={material} project={project} index={index} />
        </details>
      </div>
    </li>
  );
}

// What is known of a paper: its file, its reading, whether it is in the search index, its details'
// source and its retraction check.
export function Facts({ material, project, index }) {
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
    index && extraction && [t('search.indexTitle'), t(...paperIndexed(index, material.id))],
    [t('library.fact.details'), detailsSource(t, material, project, date)],
    latestLookup(t, material) && [t('library.fact.lookup'), latestLookup(t, material)],
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

// S1-17: search and the search index

// The project's search index status (backend/search.py), read with the papers and again while an
// index run runs; null until read, or when it cannot be.
export function useIndex(projectId, listing) {
  const [index, setIndex] = useState(null);
  const [asked] = useState(newest);
  const load = useCallback(async () => {
    const current = asked();
    if (!projectId) return;
    try {
      const found = await indexStatus(projectId);
      if (current()) setIndex(found);
    } catch {
      if (current()) setIndex(null);
    }
  }, [projectId, asked]);
  useEffect(() => { setIndex(null); }, [projectId]);
  useEffect(() => { load(); }, [load, listing]);
  useEffect(() => {
    if (!indexBusy(index)) return undefined;
    const timer = setTimeout(load, POLL_MS);
    return () => clearTimeout(timer);
  }, [index, load]);
  return index;
}

// The search field's text and the newest search's results; a search asked before a later one, or
// for another project, is dropped.
function useSearch(projectId) {
  const [text, setText] = useState('');
  const [shown, setShown] = useState(null); // { query, found }
  const [asked] = useState(newest);
  const { busy, problem, run, reset } = useAction();
  useEffect(() => { asked(); setText(''); setShown(null); reset(); }, [projectId]);
  async function search() {
    const query = queryOf(text);
    if (!query || !projectId) return;
    const current = asked();
    const found = await run(() => searchProject(projectId, query));
    if (found && current()) setShown({ query, found });
  }
  function clear() {
    asked();
    setText('');
    setShown(null);
    reset();
  }
  return { text, setText, shown, busy, problem, search, clear };
}

// The search field: Enter searches (not while an input method composes), Escape clears.
function SearchField({ search }) {
  const t = useT();
  const id = useId();
  function onKeyDown(event) {
    const action = fieldKey({ key: event.key, isComposing: event.nativeEvent.isComposing, keyCode: event.keyCode });
    if (action === 'clear') {
      event.preventDefault();
      search.clear();
    } else if (event.key === 'Enter' && action === null) {
      event.preventDefault(); // confirming composed characters, not searching
    }
  }
  return (
    <form role="search" className="flex shrink-0 items-center gap-1.5 border-b px-4 py-2"
      onSubmit={(event) => { event.preventDefault(); search.search(); }}>
      <label htmlFor={id} className="sr-only">{t('search.label')}</label>
      <div className="relative min-w-0 flex-1">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
        <Input id={id} value={search.text} placeholder={t('search.placeholder')} autoComplete="off" spellCheck={false}
          className="h-8 pl-8" onChange={(event) => search.setText(event.target.value)} onKeyDown={onKeyDown} />
      </div>
      {(search.shown || search.text) && (
        <Button type="button" size="icon" variant="ghost" className="size-8" title={t('search.clear')} aria-label={t('search.clear')}
          onClick={search.clear}><X aria-hidden="true" /></Button>
      )}
      <Button type="submit" size="sm" variant="outline" className="h-8" disabled={search.busy || !queryOf(search.text)}>
        {t('search.submit')}
      </Button>
    </form>
  );
}

// The newest search's passages, in rank order (never a score), each opening its paper at it, with
// what the search says of how they were found.
function SearchResults({ search, project, onOpen }) {
  const t = useT();
  const { shown, busy, problem } = search;
  if (!shown && !busy && !problem) return null;
  const note = shown && searchNote(shown.found, downloadOffered(project));
  const keywordOnly = shown?.found.mode === 'keyword_only';
  const results = shown?.found.results ?? [];
  return (
    <section className="grid gap-3" aria-label={t('search.label')}>
      {problem && <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{problem}</p>}
      {busy && <p role="status" className="text-sm text-muted-foreground">{t('search.searching')}</p>}
      {shown && (
        <>
          <div className="flex items-center justify-between gap-2">
            <p aria-live="polite" className="text-xs text-muted-foreground">{t('search.results', { count: results.length })}</p>
            <Button variant="ghost" size="sm" className="h-7" onClick={search.clear}>{t('search.backToPapers')}</Button>
          </div>
          {note && (
            <p role="status" className={cn('flex gap-2 rounded-md border px-3 py-2 text-xs leading-relaxed',
              keywordOnly ? 'border-warning/40 text-warning' : 'text-muted-foreground')}>
              {keywordOnly && <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />}
              <span className="min-w-0">{t(note[0], note[1].reasonKey ? { reason: t(note[1].reasonKey) } : note[1])}</span>
            </p>
          )}
          {results.length === 0 ? <p className="text-sm text-muted-foreground">{t('search.none')}</p> : (
            <ol className="divide-y rounded-lg border">
              {results.map((result) => (
                <li key={result.passage_id}>
                  <button type="button" onClick={() => onOpen(result)} title={t('search.open', { title: result.title })}
                    className="grid w-full gap-1 px-3 py-2.5 text-left transition-colors hover:bg-accent/60 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                    <span className="flex min-w-0 items-baseline gap-2">
                      <span className="min-w-0 truncate text-sm font-medium">{result.title}</span>
                      {result.kind !== 'paragraph' && (
                        <span className="shrink-0 text-[11px] font-medium uppercase tracking-wide text-brand">{t(`paper.kind.${result.kind}`)}</span>
                      )}
                    </span>
                    {whereIs(t, result) && <span className="truncate text-xs text-muted-foreground">{whereIs(t, result)}</span>}
                    <span className="line-clamp-3 break-words text-sm leading-relaxed text-foreground/90">{result.text}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
