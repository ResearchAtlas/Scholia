// i18n: migrated
// The side panel (S6): the Library or a manuscript, with a close button. Their contents
// come in later PRs (S7 and S8); until then each says what will appear. When it slides over
// the conversation, focus moves into it and Escape closes it.
import { useEffect, useRef } from 'react';
import { BookOpen, FileText, X } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { Button } from '@/components/ui/button';

export function SidePanel({ which, overlay, onClose }) {
  const t = useT();
  const close = useRef(null);
  const library = which === 'library';
  useEffect(() => { // covering the conversation (opened, or the window narrowed), it takes focus
    if (overlay) close.current?.focus();
  }, [overlay]);
  const Icon = library ? BookOpen : FileText;
  return (
    <aside className="flex h-full flex-col" aria-label={library ? t('panel.library') : t('panel.manuscript')}
      onKeyDown={overlay ? (event) => event.key === 'Escape' && onClose() : undefined}>
      <header className="flex h-12 shrink-0 items-center gap-2 border-b pl-4 pr-2">
        <h2 className="flex-1 text-sm font-medium">{library ? t('panel.library') : t('panel.manuscript')}</h2>
        <Button ref={close} variant="ghost" size="icon" className="size-8" onClick={onClose} aria-label={t('panel.close')}>
          <X aria-hidden="true" />
        </Button>
      </header>
      <div className="grid flex-1 place-items-center p-8 text-center">
        <div className="max-w-xs">
          <Icon className="mx-auto size-8 text-muted-foreground/60" aria-hidden="true" />
          <p className={library ? 'mt-3 text-sm text-muted-foreground' : 'mt-3 font-serif text-sm text-muted-foreground'}>
            {library ? t('panel.libraryEmpty') : t('panel.manuscriptEmpty')}
          </p>
        </div>
      </div>
    </aside>
  );
}
