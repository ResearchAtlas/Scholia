// i18n: migrated
// The parts of the window loaded when first opened (parts.js): Settings, a paper's page and the
// Markdown of answers. While one loads, a status says so, shown only once loading takes a moment,
// so a quick load flashes nothing and nothing moves. One that cannot load says so where it would
// show, with Try again. A browser keeps a script that failed to load as failed while the page is
// open, so Try again reloads the window, which keeps its session (session.js).
import { useState } from 'react';
import { useT } from '../i18n/index.js';
import { Boundary, part } from '../parts.js';
import { LoadState } from './fields.jsx';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

const LazySettings = part(() => import('./Settings.jsx'), 'Settings');
const LazyPaper = part(() => import('./Paper.jsx'), 'Paper');

let markdown = null; // react-markdown's component, once loaded
let markdownLoad = null;
const loadMarkdown = () => (markdownLoad ??= import('react-markdown').then((module) => { markdown = module.default; return module; }));
const LazyMarkdown = part(loadMarkdown, 'default');

// Starts loading the Markdown renderer, once; settles when it has loaded or failed (an answer
// then says it could not be loaded), never rejecting.
export function markdownReady() {
  return loadMarkdown().then(() => {}, () => {});
}

// Loading: announced at once, shown only once it takes a moment.
function Pending({ className }) {
  const t = useT();
  return (
    <p role="status" className={cn('text-sm text-muted-foreground animate-in fade-in-0 fill-mode-backwards delay-300', className)}>
      {t('common.loading')}
    </p>
  );
}

function Unavailable() {
  return <LoadState problem="part_not_loaded" onRetry={() => window.location.reload()} />;
}

// Settings, loaded when first opened and kept, so it closes as before. It is a modal: while it
// loads nothing in the window moves; if it cannot load, a dialog in its place says so.
export function Settings(props) {
  const t = useT();
  const [opened, setOpened] = useState(false);
  if (props.open && !opened) setOpened(true);
  if (!opened) return null;
  const failed = (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogTitle>{t('settings.title')}</DialogTitle>
        <DialogDescription className="sr-only">{t('settings.description')}</DialogDescription>
        <Unavailable />
      </DialogContent>
    </Dialog>
  );
  return <Boundary fallback={<Pending className="sr-only" />} failed={failed}><LazySettings {...props} /></Boundary>;
}

// A paper's page, in the panel where the Library was.
export function Paper(props) {
  return (
    <Boundary fallback={<Pending className="p-4" />} failed={<div className="p-4"><Unavailable /></div>}>
      <LazyPaper {...props} />
    </Boundary>
  );
}

// An answer's Markdown. Once the renderer has loaded (markdownReady), it is drawn at once.
export function Markdown(props) {
  const Loaded = markdown;
  if (Loaded) return <Loaded {...props} />;
  return <Boundary fallback={<Pending />} failed={<Unavailable />}><LazyMarkdown {...props} /></Boundary>;
}
