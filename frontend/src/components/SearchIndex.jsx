// i18n: migrated
// The project's search index under Settings, This project (S1-17; slice-1 spec sections 4.3 and 5):
// whether it is up to date or being rebuilt, how search runs now (by keywords and meaning, or by
// keywords only and why), its passages indexed and embedded, and Rebuild. "Rebuilt" is said only once
// the rebuild's run has succeeded, its keyword rows and its embeddings committed.
import { useCallback, useEffect, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { useAction } from '../action.js';
import { downloadOffered, keywordOnlyReason } from '../helper.js';
import { fraction, followRun } from '../runs.js';
import { indexBusy, indexStatus, rebuildIndex } from '../search.js';
import { LoadState } from './fields.jsx';
import { Button } from '@/components/ui/button';

const POLL_MS = 1500;

export function IndexSection({ project }) {
  const t = useT();
  const [index, setIndex] = useState(null);
  const [problem, setProblem] = useState(null);
  const [outcome, setOutcome] = useState(null); // the last rebuild's end: 'rebuilt' or 'stopped'
  const { busy, problem: refused, run } = useAction();
  const load = useCallback(() => indexStatus(project.id).then((found) => { setIndex(found); setProblem(null); })
    .catch((error) => setProblem(error?.code ?? 'internal')), [project.id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!indexBusy(index)) return undefined;
    const timer = setTimeout(load, POLL_MS);
    return () => clearTimeout(timer);
  }, [index, load]);

  async function rebuild() {
    setOutcome(null);
    const started = await run(() => rebuildIndex(project.id));
    if (!started) return;
    load();
    const ended = await followRun(started.run_id, () => load());
    setOutcome(ended.status === 'succeeded' ? 'rebuilt' : 'stopped');
    load();
  }

  if (!index) return <LoadState problem={problem} onRetry={load} />;
  const reason = index.mode === 'keyword_only'
    ? t('helper.keywordOnly', { reason: t(keywordOnlyReason({ search: index }, downloadOffered(project)) ?? 'helper.reason.other') })
    : t('search.indexHybrid');
  const rebuilding = busy || (index.run?.rebuild && index.run.status === 'running');
  const share = rebuilding ? fraction(index.run?.progress) : null;
  const rows = [
    [t('search.indexState'), t(`search.indexState.${index.state}`)],
    [t('search.indexMode'), reason],
    [t('search.indexPassages'), t('search.indexCounts', index.passages)],
  ];
  return (
    <div className="grid gap-3">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-lg border px-3 py-2.5 text-sm">
        {rows.map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" disabled={rebuilding} onClick={rebuild}>
          <RotateCcw aria-hidden="true" />{rebuilding ? t('search.rebuilding') : t('search.rebuild')}
        </Button>
        {rebuilding && share !== null && (
          <span className="text-xs tabular-nums text-muted-foreground" role="status">
            {t('runs.progress', { done: index.run.progress.done, total: index.run.progress.total })}
          </span>
        )}
        {!rebuilding && outcome === 'rebuilt' && <span role="status" className="text-sm text-success">{t('search.rebuilt')}</span>}
        {!rebuilding && outcome === 'stopped' && <span role="status" className="text-sm text-warning">{t('search.rebuildStopped')}</span>}
      </div>
      {refused && <p role="alert" className="text-sm text-destructive">{refused}</p>}
    </div>
  );
}
