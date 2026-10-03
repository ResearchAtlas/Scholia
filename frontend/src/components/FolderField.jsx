// i18n: migrated
// A folder named by its full path: typed, or picked in the system's dialog inside the app's
// window (backend/desktop.py WindowApi). A browser has no picker, so there it is typed.
import { useEffect, useId, useState } from 'react';
import { FolderOpen } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { folderPicker } from '../backups.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export function FolderField({ label, hint, value, onChange, placeholder }) {
  const t = useT();
  const id = useId();
  const [pick, setPick] = useState(() => folderPicker());
  useEffect(() => { // pywebview adds its bridge after the page has loaded
    const ready = () => setPick(() => folderPicker());
    window.addEventListener('pywebviewready', ready);
    return () => window.removeEventListener('pywebviewready', ready);
  }, []);

  async function choose() {
    const chosen = await pick().catch(() => null);
    if (chosen) onChange(chosen);
  }

  return (
    <div className="grid gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      <div className="flex gap-2">
        <Input id={id} value={value} spellCheck={false} autoComplete="off" className="font-mono text-xs md:text-xs"
          placeholder={placeholder ?? t('folder.placeholder')} aria-describedby={hint ? `${id}-hint` : undefined}
          onChange={(event) => onChange(event.target.value)} />
        {pick && (
          <Button type="button" variant="outline" className="shrink-0" onClick={choose}>
            <FolderOpen aria-hidden="true" />{t('folder.choose')}
          </Button>
        )}
      </div>
      {hint && <p id={`${id}-hint`} className="text-xs leading-relaxed text-muted-foreground">{hint}</p>}
    </div>
  );
}
