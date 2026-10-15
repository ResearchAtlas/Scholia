// i18n: migrated
// The Zotero browser (S11; slice-1 spec F3c; S1-15): Zotero's connection status, its libraries, the
// chosen library's collections and a page of its items with checkboxes, and the import of the items
// chosen. Loaded when first opened (Parts.jsx). What an import did is said only from its run's record once it has
// ended (followRun), never before its commit.
import { useCallback, useContext, useEffect, useId, useState } from 'react';
import { BookOpen, FolderClosed, Loader2, RefreshCw } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError } from '../api.js';
import { errorText } from '../text.js';
import { followRun, runOutcome } from '../runs.js';
import { libraryChanged } from '../library.js';
import { collectionTree, importZotero, MAX_ITEMS, reportParts, statusKey, zoteroCollections, zoteroItems,
  zoteroStatus } from '../zotero.js';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

const codeOf = (error) => (error instanceof ApiError ? error.code : 'internal');

export function ZoteroBrowser({ open, project, onOpenChange }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const libraryId = useId();
  const [status, setStatus] = useState(null);
  const [library, setLibrary] = useState('users/0');
  const [collections, setCollections] = useState(null);
  const [collection, setCollection] = useState(null); // null: all items
  const [page, setPage] = useState(null); // { items, total, next }
  const [chosen, setChosen] = useState([]); // item keys, in the order chosen
  const [problem, setProblem] = useState(null);
  const [importing, setImporting] = useState(null); // { done, total } while an import runs
  const [report, setReport] = useState(null); // [[key, params]] or { problem } once it ended

  const check = useCallback(async () => {
    setStatus(null);
    setProblem(null);
    try {
      setStatus(await zoteroStatus());
    } catch (error) {
      setStatus({ state: 'error' });
      setProblem(codeOf(error));
    }
  }, []);
  useEffect(() => {
    if (open) { setReport(null); setChosen([]); check(); }
  }, [open, check]);

  const ready = status?.state === 'ready';
  useEffect(() => { // the library's collections, once Zotero is ready
    if (!ready) return undefined;
    let live = true;
    setCollections(null);
    zoteroCollections(library).then((found) => { if (live) setCollections(found.collections); })
      .catch((error) => { if (live) { setCollections([]); setProblem(codeOf(error)); } });
    return () => { live = false; };
  }, [ready, library]);
  useEffect(() => { // the first page of the library's (or the collection's) items
    if (!ready) return undefined;
    let live = true;
    setPage(null);
    zoteroItems(library, collection).then((found) => { if (live) setPage(found); })
      .catch((error) => { if (live) { setPage({ items: [], total: 0, next: null }); setProblem(codeOf(error)); } });
    return () => { live = false; };
  }, [ready, library, collection]);

  async function more() {
    try {
      const found = await zoteroItems(library, collection, page.next);
      setPage((shown) => ({ ...found, items: [...shown.items, ...found.items] }));
    } catch (error) {
      setProblem(codeOf(error));
    }
  }

  function choose(key, on) {
    setChosen((keys) => (on ? [...keys.filter((k) => k !== key), key] : keys.filter((k) => k !== key)));
  }

  async function start() {
    setProblem(null);
    setImporting({ done: 0, total: chosen.length });
    try {
      const { run_id: runId } = await importZotero(project.id, library, chosen, status.server_id);
      const row = await followRun(runId, (running) => { if (running.progress) setImporting(running.progress); });
      const outcome = runOutcome(row);
      setReport({ parts: reportParts(row.result), problem: outcome.ok ? null : outcome.code ?? null,
        key: outcome.ok ? null : outcome.key ?? null });
      setChosen([]);
    } catch (error) {
      setReport({ parts: [], problem: codeOf(error) });
    } finally {
      setImporting(null);
      libraryChanged(project.id);
    }
  }

  const tree = collections ? collectionTree(collections, new Intl.Collator(language).compare) : [];
  const items = page?.items ?? [];
  const over = chosen.length > MAX_ITEMS;
  const libraries = status?.libraries ?? [];
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!importing) onOpenChange(next); }}>
      <DialogContent className="flex max-h-[min(88vh,760px)] max-w-3xl flex-col gap-4">
        <DialogHeader>
          <DialogTitle>{t('zotero.title')}</DialogTitle>
          <DialogDescription>{t('zotero.description')}</DialogDescription>
        </DialogHeader>
        <div className="flex items-start gap-3 rounded-lg border px-3 py-2 text-sm" data-testid="zotero-status">
          <span aria-hidden="true" className={cn('mt-1.5 size-2 shrink-0 rounded-full', ready ? 'bg-success'
            : status ? 'bg-warning' : 'bg-muted-foreground')} />
          <div className="grid min-w-0 flex-1 gap-1">
            <p role="status" className="font-medium">{t(statusKey(status))}</p>
            {status?.state === 'api_off' && <p className="text-muted-foreground">{t('zotero.howToTurnOn')}</p>}
            {status?.state === 'not_running' && <p className="text-muted-foreground">{t('zotero.howToStart')}</p>}
            {status?.state === 'unsupported' && <p className="text-muted-foreground">{t('zotero.howToUpdate')}</p>}
            {status?.state === 'off' && <p className="text-muted-foreground">{t('zotero.howToSwitchOn')}</p>}
          </div>
          <Button type="button" size="sm" variant="outline" className="h-7 shrink-0" onClick={check}
            disabled={!status || Boolean(importing)}>
            <RefreshCw aria-hidden="true" />{t('zotero.checkAgain')}
          </Button>
        </div>
        {problem && <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorText(t, problem)}</p>}
        {ready && !report && (
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 md:grid-cols-[14rem_minmax(0,1fr)]">
            <div className="grid min-h-0 content-start gap-2">
              <label htmlFor={libraryId} className="text-xs font-medium text-muted-foreground">{t('zotero.library')}</label>
              <select id={libraryId} value={library} disabled={Boolean(importing)}
                onChange={(event) => { setLibrary(event.target.value); setCollection(null); setChosen([]); }}
                className="h-8 rounded-md border bg-background px-2 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
                {libraries.map((l) => <option key={l.id} value={l.id}>{l.name ?? t('zotero.myLibrary')}</option>)}
              </select>
              <nav aria-label={t('zotero.collections')} className="scroll-thin max-h-40 overflow-y-auto rounded-md border p-1 md:max-h-80">
                {[{ key: null, name: t('zotero.allItems'), depth: 0 }, ...tree].map((c) => (
                  <button key={c.key ?? 'all'} type="button" onClick={() => setCollection(c.key)} title={c.name}
                    aria-current={collection === c.key ? 'true' : undefined} disabled={Boolean(importing)}
                    style={{ paddingInlineStart: `${0.5 + Math.min(c.depth, 6) * 0.75}rem` }}
                    className={cn('flex w-full items-center gap-1.5 truncate rounded-sm py-1 pr-2 text-left text-sm hover:bg-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
                      collection === c.key && 'bg-accent font-medium')}>
                    {c.key ? <FolderClosed className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                      : <BookOpen className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />}
                    <span className="truncate">{c.name}</span>
                  </button>
                ))}
                {collections === null && <p className="px-2 py-1 text-xs text-muted-foreground">{t('common.loading')}</p>}
              </nav>
            </div>
            <div className="grid min-h-0 content-start gap-2">
              <p className="text-xs text-muted-foreground" aria-live="polite">
                {page ? t('zotero.itemCount', { count: page.total ?? items.length }) : t('common.loading')}
              </p>
              <ul aria-label={t('zotero.items')} className="scroll-thin max-h-[42vh] divide-y overflow-y-auto rounded-md border">
                {items.map((item) => {
                  const id = `zotero-item-${item.key}`;
                  return (
                    <li key={item.key} className="flex items-start gap-2.5 px-3 py-2">
                      <input id={id} type="checkbox" className="mt-0.5 size-4 shrink-0 accent-[hsl(var(--brand))]"
                        checked={chosen.includes(item.key)} disabled={Boolean(importing)}
                        onChange={(event) => choose(item.key, event.target.checked)} />
                      <label htmlFor={id} className="grid min-w-0 flex-1 cursor-pointer gap-0.5 text-sm">
                        <span className="line-clamp-2 break-words font-medium" title={item.title ?? ''}>
                          {item.title ?? t('zotero.untitled')}
                        </span>
                        <span className="truncate text-xs text-muted-foreground">
                          {[item.creators, item.year].filter(Boolean).join(' · ')}
                        </span>
                      </label>
                    </li>
                  );
                })}
                {page && items.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">{t('zotero.noItems')}</li>}
              </ul>
              {page?.next != null && (
                <Button type="button" size="sm" variant="outline" className="h-7 w-fit" onClick={more} disabled={Boolean(importing)}>
                  {t('zotero.more')}
                </Button>
              )}
            </div>
          </div>
        )}
        {report && (
          <div role="status" className="grid gap-1 rounded-md bg-muted px-3 py-2 text-sm" data-testid="zotero-report">
            {(report.problem || report.key) && <p className="text-destructive">{report.problem ? errorText(t, report.problem) : t(report.key)}</p>}
            {report.parts.map(([key, params]) => <p key={key}>{t(key, params)}</p>)}
          </div>
        )}
        <DialogFooter className="items-center gap-2 sm:justify-between">
          <p className={cn('text-xs', over ? 'text-destructive' : 'text-muted-foreground')} aria-live="polite">
            {importing ? <span className="inline-flex items-center gap-1.5"><Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              {t('zotero.importing', { done: importing.done, total: importing.total })}</span>
              : over ? t('zotero.tooMany', { max: MAX_ITEMS })
                : ready && !report ? t('zotero.chosen', { count: chosen.length }) : null}
          </p>
          {report ? (
            <Button type="button" onClick={() => onOpenChange(false)}>{t('zotero.done')}</Button>
          ) : (
            <Button type="button" className="bg-brand text-brand-foreground hover:bg-brand/90"
              disabled={!ready || !project || chosen.length === 0 || over || Boolean(importing)} onClick={start}>
              {t('zotero.import', { count: chosen.length })}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
