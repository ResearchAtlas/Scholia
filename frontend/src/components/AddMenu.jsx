// i18n: migrated
// The Library's Add (S7; slice-1 spec F3): files, or a reference file (S1-14); Zotero joins it with
// S1-15. A reference file is imported once it is chosen, and what the import did is said only from the
// backend's answer, after its commit (backend/references.py).
import { useContext, useRef, useState } from 'react';
import { BookMarked, FilePlus2 } from 'lucide-react';
import { LanguageContext, useT } from '../i18n/index.js';
import { ApiError } from '../api.js';
import { errorText } from '../text.js';
import { importReferences, REFERENCE_ACCEPT, reportParts } from '../references.js';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';

// children(importing): the trigger, a button, disabled while a reference file is imported. onFiles opens
// the files' chooser; onStart and onDone(text) frame a reference file's import, text being its report or
// why it failed.
export function AddMenu({ project, onFiles, onStart, onDone, children }) {
  const t = useT();
  const language = useContext(LanguageContext);
  const chooser = useRef(null);
  const [importing, setImporting] = useState(false);

  async function chosen(file) {
    if (!file || !project) return;
    setImporting(true);
    onStart?.();
    try {
      const report = await importReferences(project.id, file);
      onDone(reportParts(report).map(([key, params]) => t(key, params)).join(language.startsWith('zh') ? '' : ' '));
    } catch (error) {
      onDone(errorText(t, error instanceof ApiError ? error.code : 'internal'));
    } finally {
      setImporting(false);
    }
  }

  return (
    <>
      <input ref={chooser} type="file" accept={REFERENCE_ACCEPT} className="hidden" aria-hidden="true" tabIndex={-1}
        data-testid="library-reference-file" onChange={(event) => { chosen(event.target.files?.[0]); event.target.value = ''; }} />
      {importing && <span role="status" className="text-xs text-muted-foreground">{t('references.importing')}</span>}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>{children(importing)}</DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuItem onSelect={onFiles}>
            <FilePlus2 aria-hidden="true" />{t('library.addMenu.files')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => chooser.current?.click()}>
            <BookMarked aria-hidden="true" />{t('library.addMenu.referenceFile')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}
