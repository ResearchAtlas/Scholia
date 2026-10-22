// i18n: migrated
// Where we left off (S1-21a; slice-1 spec F10 and S5): at the top of the conversation opened after at least 4 hours
// away from its project (backend/memory.py left_off), the last things done, open questions, one suggested next step
// whose button puts it in the message box (nothing is sent until Send), and Things to confirm: each pending proposal
// with Keep, Edit or Forget, at the version shown. Shown once per return; loaded when first drawn (Parts.jsx).
import { useCallback, useEffect, useState } from 'react';
import { History, X } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { openPlace } from '../live.js';
import { dismissLeftOff, fillComposer, leftOffDismissed, readLeftOff, recordText, review } from '../memory.js';
import { Proposal } from './Memory.jsx';
import { Button } from '@/components/ui/button';

export function LeftOff({ projectId }) {
  const t = useT();
  const [found, setFound] = useState(null);
  const [hidden, setHidden] = useState(false);
  const load = useCallback(() => readLeftOff(projectId).then(setFound, () => setFound(null)), [projectId]);
  useEffect(() => { setFound(null); setHidden(false); load(); }, [load]);
  if (!found?.show || hidden || leftOffDismissed(projectId, found.latest_turn_at)) return null;
  const empty = !found.done.length && !found.questions.length && !found.next_step && !found.to_confirm.length && !found.notice;
  if (empty) return null;
  return (
    <section aria-label={t('leftOff.title')} className="mb-8 grid gap-4 rounded-xl border bg-card p-4 text-sm shadow-xs">
      <div className="flex items-center gap-2">
        <History className="size-4 text-brand" aria-hidden="true" />
        <h2 className="flex-1 font-medium">{t('leftOff.title')}</h2>
        <Button variant="ghost" size="icon" className="size-7" aria-label={t('leftOff.dismiss')}
          onClick={() => { dismissLeftOff(projectId, found.latest_turn_at); setHidden(true); }}>
          <X aria-hidden="true" />
        </Button>
      </div>
      {found.done.length > 0 && <Listed title={t('leftOff.done')} items={found.done.map((r) => recordText(t, r))} />}
      {found.questions.length > 0 && <Listed title={t('leftOff.questions')} items={found.questions.map((r) => r.content)} />}
      {found.next_step && (
        <div className="grid gap-1.5">
          <h3 className="text-xs font-medium text-muted-foreground">{t('leftOff.next')}</h3>
          <div className="flex flex-wrap items-center gap-2">
            <p className="min-w-0 flex-1 break-words">{found.next_step.content}</p>
            <Button size="sm" variant="outline" className="h-7" onClick={() => fillComposer(found.next_step.content)}>
              {t('leftOff.ask')}</Button>
          </div>
        </div>
      )}
      {found.to_confirm.length > 0 && (
        <div className="grid gap-2">
          <h3 className="text-xs font-medium text-muted-foreground">{t('leftOff.confirm')}</h3>
          <ul className="grid gap-2">
            {found.to_confirm.map((proposal) => <Proposal key={proposal.id} proposal={proposal} onChanged={load} />)}
          </ul>
        </div>
      )}
      {found.notice && (
        <div role="status" className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <p className="min-w-0 flex-1">{t('leftOff.notice')}</p>
          <Button size="sm" variant="outline" className="h-7" onClick={() => openPlace('settings.advanced')}>
            {t('leftOff.chooseExtractor')}</Button>
          <Button size="sm" variant="ghost" className="h-7" onClick={async () => {
            await review({ project_id: projectId, dismiss: 'allowance_notice' }).catch(() => {});
            load();
          }}>{t('leftOff.dismissNotice')}</Button>
        </div>
      )}
    </section>
  );
}

function Listed({ title, items }) {
  return (
    <div className="grid gap-1">
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      <ul className="list-disc space-y-0.5 pl-5">{items.map((item, i) => <li key={i} className="break-words">{item}</li>)}</ul>
    </div>
  );
}
