// i18n: migrated
// Scholia's mark: an S on the indigo accent.
import { useT } from '../i18n/index.js';
import { cn } from '@/lib/utils';

export function Mark({ className }) {
  const t = useT();
  return (
    <div aria-hidden="true"
      className={cn('grid shrink-0 place-items-center rounded-lg bg-linear-to-br/srgb from-brand to-violet-500 font-semibold text-white shadow-xs', className)}>
      {t('app.name').charAt(0)}
    </div>
  );
}
