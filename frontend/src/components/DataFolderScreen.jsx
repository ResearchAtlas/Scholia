// i18n: migrated
// The data-folder screen (S2): shown only when Scholia will not open its data folder. The
// backend reports why in /api/health (data_folder_problem: "synced", "unsafe" or "missing"),
// and records another place for the next launch (POST /api/data-folder, backend/data_folder.py).
import { useState } from 'react';
import { CheckCircle2, FolderX } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { post } from '../api.js';
import { useAction } from '../action.js';
import { FolderField } from './FolderField.jsx';
import { Button } from '@/components/ui/button';

const REASONS = { synced: 'dataFolder.synced', unsafe: 'dataFolder.unsafe', missing: 'dataFolder.missing' };

export function DataFolderScreen({ health }) {
  const t = useT();
  const [path, setPath] = useState('');
  const [chosen, setChosen] = useState(null);
  const { busy, problem, run } = useAction();

  async function submit(event) {
    event.preventDefault();
    const result = await run(() => post('/api/data-folder', { path: path.trim() }));
    if (result) setChosen(result.data_folder);
  }

  return (
    <main className="grid h-full place-items-center overflow-y-auto p-6">
      <div className="w-full max-w-lg rounded-2xl border bg-card p-8 shadow-xl">
        <FolderX className="size-8 text-warning" aria-hidden="true" />
        <h1 className="mt-4 text-xl font-semibold tracking-tight">{t('dataFolder.title')}</h1>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
          {t(REASONS[health.data_folder_problem] ?? 'dataFolder.unsafe')}
        </p>
        <p className="mt-5 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('dataFolder.where')}</p>
        <p className="mt-1 break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">{health.data_folder}</p>

        {chosen ? (
          <div role="status" className="mt-6 border-t pt-5">
            <p className="flex gap-2 text-sm leading-relaxed">
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />
              <span>{t('dataFolder.chosen')}</span>
            </p>
            <p className="mt-2 break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">{chosen}</p>
          </div>
        ) : (
          <form onSubmit={submit} className="mt-6 grid gap-3 border-t pt-5">
            <h2 className="text-sm font-semibold">{t('dataFolder.chooseTitle')}</h2>
            <FolderField label={t('folder.label')} hint={t('dataFolder.chooseHint')} value={path} onChange={setPath} />
            {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
            <Button type="submit" className="justify-self-start" disabled={busy || !path.trim()}>{t('dataFolder.use')}</Button>
          </form>
        )}
      </div>
    </main>
  );
}
