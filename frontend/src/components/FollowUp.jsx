// i18n: migrated
// S1-33 (slice-1 spec F11, S5): the budget bar above the message box, the messages waiting as the next turn, and a
// turn's steering notes, each as the record says (backend/app.py): a note is said to be read only once a step that
// carried it went out, and a message is said to wait only once the queue holds it.
import { useContext, useEffect, useState } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { del, get, put } from '../api.js';
import { useAction } from '../action.js';
import { budgetBar } from '../followup.js';
import { money, visible } from '../text.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

// Read again whenever `seen` changes (a turn's read, a budget notice): settled spend, as the record says now.
export function BudgetBar({ projectId, conversationId, seen }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const [spend, setSpend] = useState(null);
  useEffect(() => {
    if (!projectId || !conversationId) return undefined;
    let live = true;
    get(`/api/projects/${encodeURIComponent(projectId)}/spend?conversation_id=${encodeURIComponent(conversationId)}`)
      .then((found) => live && setSpend(found), () => {}); // a failed read keeps what was shown
    return () => { live = false; };
  }, [projectId, conversationId, seen]);
  const bar = budgetBar(spend);
  if (!bar) return null;
  const label = t(`budget.bar.${bar.budget}`, { spent: money(bar.settled, language), limit: money(bar.limit, language) });
  return (
    <div className="grid gap-1 px-2 pb-1.5 text-[11px] text-muted-foreground">
      <div role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(bar.percent)}
        className="relative h-1 overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full rounded-full', bar.percent >= 90 ? 'bg-warning' : 'bg-brand/70')}
          style={{ width: `${Math.min(bar.percent, 100)}%` }} />
        {bar.marks.map((mark) => <span key={mark} className="absolute inset-y-0 w-px bg-foreground/30" style={{ left: `${mark}%` }} />)}
      </div>
      <p>{label}</p>
    </div>
  );
}

// The messages waiting as the next turn: editable and removable; Send when no turn runs.
export function Queued({ conversationId, queued, running, onChanged, onSend }) {
  const t = useT();
  if (!queued?.length) return null;
  return (
    <ul className="grid gap-1.5 px-1 pb-2" aria-label={t('followUp.waiting')}>
      {queued.map((item) => <QueuedItem key={item.id} conversationId={conversationId} item={item} running={running}
        onChanged={onChanged} onSend={onSend} />)}
    </ul>
  );
}

function QueuedItem({ conversationId, item, running, onChanged, onSend }) {
  const t = useT();
  const [editing, setEditing] = useState(null);
  const { busy, problem, run } = useAction();
  const path = `/api/conversations/${encodeURIComponent(conversationId)}/queue/${encodeURIComponent(item.id)}`;
  const changed = async (action) => {
    if ((await run(action)) !== undefined) { setEditing(null); onChanged(); } else onChanged();
  };
  return (
    <li className="grid gap-1 rounded-lg border border-dashed bg-muted/40 px-3 py-2 text-sm">
      <p className="text-[11px] text-muted-foreground">{t(running ? 'followUp.queuedRunning' : 'followUp.queuedIdle')}</p>
      {editing === null ? <p className="whitespace-pre-wrap break-words">{item.text}</p> : (
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault();
          if (visible(editing)) changed(() => put(path, { content: editing })); }}>
          <label htmlFor={`queued-${item.id}`} className="sr-only">{t('followUp.editLabel')}</label>
          <Input id={`queued-${item.id}`} value={editing} onChange={(event) => setEditing(event.target.value)} className="h-8" />
          <Button type="submit" size="sm" variant="outline" disabled={busy || !visible(editing)}>{t('followUp.save')}</Button>
        </form>
      )}
      {problem && <p role="alert" className="text-xs text-destructive">{problem}</p>}
      <div className="flex flex-wrap gap-2">
        {!running && <Button type="button" size="sm" className="h-7 bg-brand text-brand-foreground hover:bg-brand/90"
          disabled={busy} onClick={() => run(() => onSend(item))}>{t('followUp.send')}</Button>}
        <Button type="button" size="sm" variant="ghost" className="h-7" disabled={busy}
          onClick={() => setEditing(editing === null ? item.text : null)}>{t(editing === null ? 'followUp.edit' : 'followUp.cancelEdit')}</Button>
        <Button type="button" size="sm" variant="ghost" className="h-7" disabled={busy}
          onClick={() => changed(() => del(path))}>{t('followUp.remove')}</Button>
      </div>
    </li>
  );
}

// A turn's steering notes, as the record says: waiting for the next step, read by one, or moved to the queue.
export function SteeringNotes({ notes }) {
  const t = useT();
  if (!notes?.length) return null;
  return (
    <ul className="flex flex-col items-end gap-1.5">
      {notes.map((note) => (
        <li key={note.seq} className="max-w-[85%] rounded-2xl rounded-br-md border bg-muted/40 px-3 py-2 text-sm">
          <span className="sr-only">{t('followUp.note')}: </span>
          <span className="whitespace-pre-wrap break-words">{note.text}</span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">{t(`followUp.state.${note.state}`)}</span>
        </li>
      ))}
    </ul>
  );
}
