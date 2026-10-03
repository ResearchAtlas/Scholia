// i18n: migrated
// Exporting a project, from its settings (S12; backend/backups.py): one zip file anyone can
// open, with its conversations as Markdown, its records as JSON, its files and its settings,
// never a key. A Private or Local only project's export is encrypted with a passphrase.
import { useState } from 'react';
import { useT } from '../i18n/index.js';
import { post } from '../api.js';
import { needsPassphrase } from '../backups.js';
import { useAction } from '../action.js';
import { FolderField } from './FolderField.jsx';
import { Passphrase, Saved } from './Backups.jsx';
import { Button } from '@/components/ui/button';

export function ProjectExportSection({ project }) {
  const t = useT();
  const [destination, setDestination] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [saved, setSaved] = useState(null);
  const { busy, problem, run } = useAction();
  const encrypted = needsPassphrase([project]);

  async function submit(event) {
    event.preventDefault();
    setSaved(null);
    const result = await run(() => post(`/api/projects/${encodeURIComponent(project.id)}/export`,
      { destination: destination.trim(), ...(passphrase ? { passphrase } : {}) }));
    if (result) { setSaved(result.file); setPassphrase(''); }
  }

  return (
    <form onSubmit={submit} className="grid gap-3">
      <FolderField label={t('folder.label')} value={destination} onChange={setDestination} />
      {encrypted && (
        <Passphrase id="export-passphrase" value={passphrase} onChange={setPassphrase} hint={t('export.passphraseHint')} />
      )}
      {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
      {busy && <p role="status" className="text-sm text-muted-foreground">{t('backups.working')}</p>}
      {saved && <Saved text={t('backups.saved', { file: saved })} />}
      <Button type="submit" className="justify-self-start"
        disabled={busy || !destination.trim() || (encrypted && !passphrase)}>{t('export.start')}</Button>
    </form>
  );
}
