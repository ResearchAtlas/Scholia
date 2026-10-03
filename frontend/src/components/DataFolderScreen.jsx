// i18n: migrated
// The data-folder screen (S2): shown only when Scholia cannot use its data folder, such as
// one inside a synced folder. The backend reports the problem in /api/health
// (data_folder_problem: "synced" or "unsafe"); S1-12 adds choosing another place.
import { FolderX } from 'lucide-react';
import { useT } from '../i18n/index.js';

export function DataFolderScreen({ health }) {
  const t = useT();
  const reason = health.data_folder_problem === 'synced' ? t('dataFolder.synced') : t('dataFolder.unsafe');
  return (
    <main className="grid h-full place-items-center p-6">
      <div className="w-full max-w-lg rounded-2xl border bg-card p-8 shadow-xl">
        <FolderX className="size-8 text-warning" aria-hidden="true" />
        <h1 className="mt-4 text-xl font-semibold tracking-tight">{t('dataFolder.title')}</h1>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{reason}</p>
        <p className="mt-5 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('dataFolder.where')}</p>
        <p className="mt-1 break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">{health.data_folder}</p>
      </div>
    </main>
  );
}
