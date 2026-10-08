// i18n: migrated
// The delete dialog (slice-1 spec F13, S14): Delete, Delete everywhere including backups, or
// Cancel, with the backup retention note, and "Remove all trace" under Details. Deleting
// everywhere takes a fresh backup and removes the older ones, so the deleted data is in none;
// removing all trace keeps no title in the record that something was deleted.
import { useEffect, useId, useRef, useState } from 'react';
import { useT } from '../i18n/index.js';
import { api } from '../api.js';
import { deletePath, deletionNotices } from '../backups.js';
import { useAction } from '../action.js';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

// target: { kind: "project", "conversation" or "material", id, title, body }, or null when closed.
export function DeleteDialog({ target, onClose, onDone }) {
  const t = useT();
  const traceId = useId();
  const [removeAllTrace, setRemoveAllTrace] = useState(false);
  const [notices, setNotices] = useState([]); // what a deletion that succeeded must still say
  const { busy, problem, run, reset } = useAction();
  const shown = useRef(target); // the last target, so the dialog keeps its text while it closes
  if (target) shown.current = target;
  const key = target ? `${target.kind}:${target.id}` : null;
  useEffect(() => { // each deletion starts afresh; a closing dialog keeps what it shows
    if (key) { setRemoveAllTrace(false); setNotices([]); }
  }, [key]);

  async function remove(everywhere) {
    const result = await run(() => api('DELETE', deletePath(target.kind, target.id, { everywhere, removeAllTrace })));
    if (!result) return;
    const said = deletionNotices(result);
    if (said.length) setNotices(said); // deleted, but not wholly: say so before it closes
    else onDone(target.id);
  }

  function close() {
    if (busy) return;
    reset();
    if (notices.length) onDone(target.id);
    else onClose();
  }

  return (
    <Dialog open={Boolean(target)} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{shown.current?.title}</DialogTitle>
          <DialogDescription>{shown.current?.body}</DialogDescription>
        </DialogHeader>
        {notices.length > 0 ? (
          <>
            <div role="alert" className="grid gap-2 text-sm text-warning">
              {notices.map((notice) => <p key={notice}>{t(notice)}</p>)}
            </div>
            <DialogFooter><Button type="button" onClick={close}>{t('common.close')}</Button></DialogFooter>
          </>
        ) : (
          <>
            <p className="text-sm leading-relaxed text-muted-foreground">{t('delete.retention')}</p>
            <details className="group rounded-md border px-3 py-2 text-sm">
              <summary className="cursor-pointer select-none font-medium">{t('delete.details')}</summary>
              <div className="mt-2 flex items-start gap-2.5">
                <input id={traceId} type="checkbox" checked={removeAllTrace} disabled={busy}
                  onChange={(event) => setRemoveAllTrace(event.target.checked)}
                  className="mt-0.5 size-4 accent-[hsl(var(--brand))]" />
                <label htmlFor={traceId} className="grid gap-0.5">
                  <span>{t('delete.removeAllTrace')}</span>
                  <span className="text-xs text-muted-foreground">{t('delete.removeAllTraceHint')}</span>
                </label>
              </div>
            </details>
            {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
            {busy && <p role="status" className="text-sm text-muted-foreground">{t('backups.working')}</p>}
            <DialogFooter className="gap-2 sm:gap-0">
              <Button type="button" variant="ghost" disabled={busy} onClick={close}>{t('common.cancel')}</Button>
              <Button type="button" variant="outline" disabled={busy} onClick={() => remove(true)}
                className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive">
                {t('delete.everywhere')}
              </Button>
              <Button type="button" variant="destructive" disabled={busy} onClick={() => remove(false)}>
                {t('common.delete')}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
