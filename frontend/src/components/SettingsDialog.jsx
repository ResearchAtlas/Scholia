// i18n: migrated
// Settings, for now Advanced only (S1-10 adds the other pages): the background runs with
// their status, cost and Cancel (slice-1 spec section 6.1, review 3 M04), and where the
// data folder is (S2, shown here when it is not in a synced folder).
import { useCallback, useContext, useEffect, useState } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError, get, post } from '../api.js';
import { errorText, money } from '../text.js';
import { projectName } from '../projects.js';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

const WORKFLOWS = { title: 'settings.workflowTitle' };
const POLL_MS = 2000; // ponytail: polled while open; a pushed event stream if the list grows

export function SettingsDialog({ open, onOpenChange, health }) {
  const t = useT();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl grid-rows-[auto_minmax(0,1fr)]">
        <DialogHeader><DialogTitle>{t('settings.title')}</DialogTitle></DialogHeader>
        <div className="scroll-thin -mx-6 space-y-8 overflow-y-auto px-6">
          <section>
            <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('settings.advanced')}</h3>
            {open && <BackgroundRuns />}
          </section>
          <section>
            <h4 className="text-sm font-medium">{t('settings.dataFolder')}</h4>
            <p className="mt-1 text-sm text-muted-foreground">{t('settings.dataFolderHint')}</p>
            <p className="mt-2 break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">{health.data_folder}</p>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function BackgroundRuns() {
  const t = useT();
  const language = useContext(LanguageContext);
  const [runs, setRuns] = useState(null);
  const [problem, setProblem] = useState(null);

  const load = useCallback(() => get('/api/activity').then((data) => setRuns(data.runs)).catch((error) => {
    setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
  }), [t]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function cancel(runId) {
    try {
      await post(`/api/runs/${runId}/cancel`);
    } catch (error) {
      setProblem(errorText(t, error instanceof ApiError ? error.code : 'internal'));
    }
    load();
  }

  const dates = new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' });
  return (
    <div className="mt-3">
      <h4 className="text-sm font-medium">{t('settings.backgroundRuns')}</h4>
      <p className="mt-1 text-sm text-muted-foreground">{t('settings.backgroundRunsHint')}</p>
      {problem && <p role="alert" className="mt-2 text-sm text-destructive">{problem}</p>}
      {runs?.length === 0 && <p className="mt-3 text-sm text-muted-foreground">{t('settings.noRuns')}</p>}
      {runs?.length > 0 && (
        <ul className="mt-3 divide-y rounded-lg border">
          {runs.map((run) => (
            <li key={run.run_id} className="flex items-center gap-3 px-3 py-2.5 text-sm">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{WORKFLOWS[run.workflow] ? t(WORKFLOWS[run.workflow]) : run.workflow}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {projectName(t, { kind: run.project_kind, name: run.project_name })} · {t('runs.started', { date: dates.format(new Date(run.started_at)) })}
                </p>
              </div>
              {run.cost_usd > 0 && <span className="text-xs tabular-nums text-muted-foreground">{t('common.cost', { cost: money(run.cost_usd, language) })}</span>}
              <span className={cn('rounded-full px-2 py-0.5 text-xs', {
                running: 'bg-brand-soft text-brand', succeeded: 'bg-success/10 text-success', failed: 'bg-destructive/10 text-destructive',
              }[run.status] ?? 'bg-muted text-muted-foreground')}>{t(`status.${run.status}`)}</span>
              {run.status === 'running' && (
                <Button size="sm" variant="outline" className="h-7" onClick={() => cancel(run.run_id)}>{t('settings.cancelRun')}</Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
