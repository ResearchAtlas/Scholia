// i18n: migrated
// The side panel (S6): the Library (S7, Library.jsx) or a manuscript, with a close button. The
// manuscript comes with S8; until then it says what will appear. When the panel slides over the
// conversation, focus moves into it and Escape closes it.
import { useEffect, useRef } from 'react';
import { FileText, X } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { Library } from './Library.jsx';
import { Button } from '@/components/ui/button';

export function SidePanel({ which, overlay, onClose, project }) {
  const t = useT();
  const close = useRef(null);
  const library = which === 'library';
  useEffect(() => { // covering the conversation (opened, or the window narrowed), it takes focus
    if (overlay) close.current?.focus();
  }, [overlay]);
  return (
    <aside className="flex h-full flex-col" aria-label={library ? t('panel.library') : t('panel.manuscript')}
      onKeyDown={overlay ? (event) => event.key === 'Escape' && onClose() : undefined}>
      <header className="flex h-12 shrink-0 items-center gap-2 border-b pl-4 pr-2">
        <h2 className="flex-1 text-sm font-medium">{library ? t('panel.library') : t('panel.manuscript')}</h2>
        <Button ref={close} variant="ghost" size="icon" className="size-8" onClick={onClose} aria-label={t('panel.close')}>
          <X aria-hidden="true" />
        </Button>
      </header>
      {/* One Library per project: nothing of one project's (a read still under way, its open paper) reaches another's. */}
      {library ? <div className="min-h-0 flex-1"><Library key={project?.id} project={project} /></div> : (
        <div className="grid flex-1 place-items-center p-8 text-center">
          <div className="max-w-xs">
            <FileText className="mx-auto size-8 text-muted-foreground/60" aria-hidden="true" />
            <p className="mt-3 font-serif text-sm text-muted-foreground">{t('panel.manuscriptEmpty')}</p>
          </div>
        </div>
      )}
    </aside>
  );
}
