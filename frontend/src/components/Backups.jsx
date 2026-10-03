// i18n: migrated
// Backups and restore, under Settings, then Advanced (S12; backend/backups.py): the automatic
// backups with Back up now and Restore, a full backup to a folder the researcher chooses, and
// restoring from a full backup file, each restore after a confirmation. DamagedDatabaseScreen
// offers the restore alone, for a database found damaged at startup.
import { useContext, useEffect, useRef, useState } from 'react';
import { CheckCircle2, DatabaseBackup, History, TriangleAlert } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { get, post } from '../api.js';
import { fileSize, restoreBody } from '../backups.js';
import { useAction } from '../action.js';
import { FolderField } from './FolderField.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

// onRestored runs after a restore, whose state the rest of the interface must load again.
export function BackupsSection({ onRestored = () => window.location.reload(), restoreOnly = false }) {
  const t = useT();
  const [backups, setBackups] = useState(null);
  const [restoring, setRestoring] = useState(null); // the restore request waiting for its confirmation
  const [restored, setRestored] = useState(null);
  const listing = useAction();
  const backingUp = useAction();

  const [listed, setListed] = useState(0); // counts listings asked for: a new backup asks again
  const { run: list } = listing;
  useEffect(() => { list(async () => setBackups((await get('/api/backups')).backups)); }, [listed]);

  async function backUpNow() {
    if (await backingUp.run(() => post('/api/backups'))) setListed((n) => n + 1);
  }

  if (restored) return <Restored result={restored} onContinue={onRestored} />;

  return (
    <div className="grid gap-6">
      <section className="grid gap-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h4 className="text-sm font-medium">{t('backups.title')}</h4>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t('backups.hint')}</p>
          </div>
          {!restoreOnly && (
            <Button variant="outline" className="shrink-0" disabled={backingUp.busy} onClick={backUpNow}>
              <DatabaseBackup aria-hidden="true" />{t('backups.backUpNow')}
            </Button>
          )}
        </div>
        {backingUp.problem && <p role="alert" className="text-sm text-destructive">{backingUp.problem}</p>}
        {listing.problem && <p role="alert" className="text-sm text-destructive">{listing.problem}</p>}
        {backups?.length === 0 && <p className="text-sm text-muted-foreground">{t('backups.none')}</p>}
        {backups?.length > 0 && (
          <ul className="scroll-thin max-h-72 divide-y overflow-y-auto rounded-lg border">
            {backups.map((backup) => <BackupRow key={backup.id} backup={backup}
              onRestore={(when) => setRestoring({ generation: backup.id, when })} />)}
          </ul>
        )}
      </section>

      {!restoreOnly && <FullBackup />}
      <RestoreFromFile onRestore={(request) => setRestoring(request)} />

      <ConfirmRestore request={restoring} damaged={restoreOnly} onClose={() => setRestoring(null)}
        onDone={(result) => { setRestoring(null); setRestored(result); }} />
    </div>
  );
}

// A database damaged at startup: Scholia changed nothing and offers only a restore.
export function DamagedDatabaseScreen({ onRestored }) {
  const t = useT();
  return (
    <main className="grid h-full place-items-center overflow-y-auto p-6">
      <div className="w-full max-w-2xl rounded-2xl border bg-card p-8 shadow-xl">
        <TriangleAlert className="size-8 text-warning" aria-hidden="true" />
        <h1 className="mt-4 text-xl font-semibold tracking-tight">{t('backups.damagedTitle')}</h1>
        <p className="mb-6 mt-2 text-sm leading-relaxed text-muted-foreground">{t('backups.damagedBody')}</p>
        <BackupsSection restoreOnly onRestored={onRestored} />
      </div>
    </main>
  );
}

function BackupRow({ backup, onRestore }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const when = new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(backup.time));
  return (
    <li className="flex items-center gap-3 px-3 py-2.5 text-sm">
      <History className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{when}</p>
        <p className="truncate text-xs text-muted-foreground">
          {t('backups.detail', { kind: t(`backups.${backup.kind}`), size: fileSize(backup.size, language) })}
        </p>
      </div>
      <Button size="sm" variant="outline" className="h-7" onClick={() => onRestore(when)}
        aria-label={t('backups.restoreThis', { time: when })}>{t('backups.restore')}</Button>
    </li>
  );
}

function FullBackup() {
  const t = useT();
  const [destination, setDestination] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [saved, setSaved] = useState(null);
  const { busy, problem, run } = useAction();

  async function submit(event) {
    event.preventDefault();
    setSaved(null);
    const result = await run(() => post('/api/backups/full',
      { destination: destination.trim(), ...(passphrase ? { passphrase } : {}) }));
    if (result) { setSaved(result.file); setPassphrase(''); }
  }

  return (
    <form onSubmit={submit} className="grid gap-3 border-t pt-5">
      <div>
        <h4 className="text-sm font-medium">{t('backups.full')}</h4>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t('backups.fullHint')}</p>
      </div>
      <FolderField label={t('folder.label')} value={destination} onChange={setDestination} />
      <Passphrase id="full-backup-passphrase" value={passphrase} onChange={setPassphrase} hint={t('backups.passphraseHint')} />
      {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
      {busy && <p role="status" className="text-sm text-muted-foreground">{t('backups.working')}</p>}
      {saved && <Saved text={t('backups.saved', { file: saved })} />}
      <Button type="submit" className="justify-self-start" disabled={busy || !destination.trim()}>{t('backups.fullStart')}</Button>
    </form>
  );
}

function RestoreFromFile({ onRestore }) {
  const t = useT();
  const [file, setFile] = useState('');
  const [passphrase, setPassphrase] = useState('');
  return (
    <form className="grid gap-3 border-t pt-5" onSubmit={(event) => {
      event.preventDefault();
      onRestore({ file, passphrase });
    }}>
      <div>
        <h4 className="text-sm font-medium">{t('backups.fromFile')}</h4>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t('backups.fromFileHint')}</p>
      </div>
      <div className="grid gap-1.5">
        <label htmlFor="restore-file" className="text-sm font-medium">{t('backups.fileLabel')}</label>
        <Input id="restore-file" value={file} spellCheck={false} autoComplete="off" className="font-mono text-xs md:text-xs"
          placeholder={t('backups.filePlaceholder')} onChange={(event) => setFile(event.target.value)} />
      </div>
      <Passphrase id="restore-passphrase" value={passphrase} onChange={setPassphrase} hint={t('backups.passphraseRestoreHint')} />
      <Button type="submit" variant="outline" className="justify-self-start" disabled={!file.trim()}>
        {t('backups.restoreFile')}
      </Button>
    </form>
  );
}

// damaged: the database is damaged, so there is no safety copy; it is moved aside instead.
function ConfirmRestore({ request, damaged, onClose, onDone }) {
  const t = useT();
  const { busy, problem, run, reset } = useAction();
  async function confirm() {
    const result = await run(() => post('/api/backups/restore', restoreBody(request)));
    if (result) onDone(result);
  }
  return (
    <Dialog open={Boolean(request)} onOpenChange={(next) => { if (!next && !busy) { reset(); onClose(); } }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('backups.restoreTitle')}</DialogTitle>
          <DialogDescription>{t(damaged ? 'backups.restoreDamagedBody' : 'backups.restoreBody')}</DialogDescription>
        </DialogHeader>
        <p className="break-all rounded-md bg-muted px-3 py-2 text-sm">
          {request?.when ? t('backups.restoreWhat', { time: request.when }) : <span className="font-mono text-xs">{request?.file}</span>}
        </p>
        {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
        {busy && <p role="status" className="text-sm text-muted-foreground">{t('backups.working')}</p>}
        <DialogFooter>
          <Button type="button" variant="ghost" disabled={busy} onClick={() => { reset(); onClose(); }}>{t('common.cancel')}</Button>
          <Button type="button" disabled={busy} onClick={confirm}>{t('backups.restoreConfirm')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Restored({ result, onContinue }) {
  const t = useT();
  const missing = result.missing_files ?? [];
  const heading = useRef(null);
  useEffect(() => heading.current?.focus(), []); // the dialog that had focus is gone
  return (
    <div className="grid gap-3">
      <div ref={heading} tabIndex={-1} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <Saved text={t('backups.restored')} />
      </div>
      {missing.length > 0 && (
        <div role="alert" className="rounded-md border border-warning/40 px-3 py-2 text-sm">
          <p className="font-medium text-warning">{t('backups.missingFiles', { count: missing.length })}</p>
          <ul tabIndex={0} aria-label={t('backups.missingFiles', { count: missing.length })}
            className="mt-1 max-h-32 overflow-y-auto rounded-sm font-mono text-xs text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {missing.map((file) => <li key={file.sha256} className="truncate" title={file.sha256}>{file.sha256}</li>)}
          </ul>
        </div>
      )}
      <Button className="justify-self-start" onClick={onContinue}>{t('backups.continue')}</Button>
    </div>
  );
}

// A passphrase for an encrypted backup or export. Scholia never stores it.
export function Passphrase({ id, value, onChange, hint }) {
  const t = useT();
  return (
    <div className="grid gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">{t('backups.passphrase')}</label>
      <Input id={id} type="password" value={value} maxLength={1024} autoComplete="new-password"
        aria-describedby={`${id}-hint`} onChange={(event) => onChange(event.target.value)} />
      <p id={`${id}-hint`} className="text-xs leading-relaxed text-muted-foreground">{hint}</p>
    </div>
  );
}

export function Saved({ text }) {
  return (
    <p role="status" className="flex gap-2 text-sm leading-relaxed">
      <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />
      <span className="min-w-0 break-words">{text}</span>
    </p>
  );
}
