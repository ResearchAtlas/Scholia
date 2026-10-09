// i18n: migrated
// Exporting a project, from its settings (S12; backend/backups.py): one zip file anyone can
// open, with its conversations as Markdown, its records as JSON, its files and its settings,
// never a key. A Private or Local only project's export is encrypted with a passphrase.
import { useContext, useState } from 'react';
import { LanguageContext, useT } from '../i18n/index.js';
import { needsPassphrase } from '../backups.js';
import { FolderField } from './FolderField.jsx';
import { ArchiveStatus, Passphrase, useArchiveRun } from './Backups.jsx';
import { Button } from '@/components/ui/button';

export function ProjectExportSection({ project }) {
  const t = useT();
  const language = useContext(LanguageContext); // the export's headings are written in it
  const [destination, setDestination] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const archive = useArchiveRun();
  const encrypted = needsPassphrase([project]);

  async function submit(event) {
    event.preventDefault();
    const sent = passphrase;
    setPassphrase(''); // held by the run in memory only, and not kept in the form either
    if (!(await archive.start(`/api/projects/${encodeURIComponent(project.id)}/export`,
      { destination: destination.trim(), language, ...(sent ? { passphrase: sent } : {}) }))) setPassphrase(sent);
  }

  return (
    <form onSubmit={submit} className="grid gap-3">
      <FolderField label={t('folder.label')} value={destination} onChange={setDestination} />
      {encrypted && (
        <Passphrase id="export-passphrase" value={passphrase} onChange={setPassphrase} hint={t('export.passphraseHint')} />
      )}
      <ArchiveStatus archive={archive} />
      <Button type="submit" className="justify-self-start"
        disabled={archive.busy || !destination.trim() || (encrypted && !passphrase)}>{t('export.start')}</Button>
    </form>
  );
}
