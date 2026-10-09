// i18n: migrated
// A paper's page (S7): its details for editing, its state and what is known of it, and its text. A
// PDF's pages are images rendered by the backend (pypdfium2) with the passages' boxes drawn over
// them; every format also shows its passages as text, in order. The cited passage's highlight comes
// with S1-19's citations; here a passage is highlighted while it is pointed at or focused, and Tab
// reaches each passage, in the text and on the page.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { ArrowLeft, FileUp, Trash2 } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { patch } from '../api.js';
import { useAction } from '../action.js';
import { visible } from '../text.js';
import { ACCEPT, byPage, changes, detailsOf, heldPages, hovering, isPdf, isPointed, libraryChanged, loadPassages,
  NOT_POINTED, pageImage, pointing, rectStyle, reasonKey, takeSaved, unionRect, validYear, viewOf, withNear } from '../library.js';
import { addTo, Byline, Facts, Progress, ReadAgain, Retracted, StateChip } from './Library.jsx';
import { DeleteDialog } from './DeleteDialog.jsx';
import { Segmented } from './fields.jsx';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { cn } from '@/lib/utils';

export function Paper({ material, project, onBack, onChanged }) {
  const t = useT();
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState(null);
  const replace = useRef(null);
  const reason = reasonKey(material.reason);
  const { busy: replacing, run } = useAction(); // one replacement at a time: none finishes after a later choice

  function replaceFile(files) {
    setNotice(null);
    return run(async () => {
      if (await addTo(project.id, [...files].slice(0, 1), t, setNotice, { materialId: material.id })) onChanged();
    });
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5">
        <Button variant="ghost" size="sm" className="h-8" onClick={onBack}>
          <ArrowLeft aria-hidden="true" />{t('paper.back')}
        </Button>
        <span className="flex-1" />
        <input ref={replace} type="file" accept={ACCEPT} className="hidden" aria-hidden="true" tabIndex={-1}
          data-testid="paper-replace" onChange={(event) => { replaceFile(event.target.files); event.target.value = ''; }} />
        <Button variant="ghost" size="sm" className="h-8" disabled={replacing} onClick={() => replace.current?.click()}>
          <FileUp aria-hidden="true" />{replacing ? t('paper.replacing') : t('paper.replace')}
        </Button>
        <Button variant="ghost" size="sm" className="h-8 text-destructive hover:text-destructive" onClick={() => setDeleting(true)}>
          <Trash2 aria-hidden="true" />{t('common.delete')}
        </Button>
      </div>
      <div className="scroll-thin min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-5">
        <header className="grid grid-cols-1 gap-2">
          <div className="flex flex-wrap items-center gap-2"><StateChip material={material} /><Retracted material={material} /></div>
          <h3 className="break-words text-lg font-semibold leading-snug tracking-tight">{material.title}</h3>
          <Byline material={material} />
          <Progress material={material} />
          {reason && <p className="text-sm text-warning">{t(reason, { count: material.extraction?.ocr_pages ?? 0 })}</p>}
          {material.readable && <ReadAgain material={material} onDone={onChanged} />}
          {notice && <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">{notice}</p>}
        </header>
        <Details material={material} onSaved={onChanged} />
        <section className="grid gap-2 text-sm">
          <h4 className="font-semibold">{t('paper.about')}</h4>
          <Facts material={material} project={project} />
        </section>
        {material.extraction && material.version && <Contents key={material.version.id} material={material} />}
      </div>
      <DeleteDialog target={deleting ? { kind: 'material', id: material.id, title: t('paper.deleteTitle', { title: material.title }),
        body: t('paper.deleteBody') } : null}
        onClose={() => setDeleting(false)}
        onDone={() => { setDeleting(false); libraryChanged(project.id); onBack(); }} />
    </div>
  );
}

// The details the researcher may correct; a save is kept as checked by the researcher, so no lookup
// replaces it, and says it was saved only once the backend has saved it.
function Details({ material, onSaved }) {
  const t = useT();
  const id = useId();
  const before = detailsOf(material);
  const [form, setForm] = useState(before);
  const [saved, setSaved] = useState(false);
  const { busy, problem, run } = useAction();
  const shown = useRef(before); // the saved details the form last took
  const key = `${material.id}:${material.updated_at}`;
  useEffect(() => { // newly saved details (a lookup, or this form's save) fill the form, but for the fields being edited
    takeSaved(shown, before, setForm);
  }, [key]); // only when the saved details change
  const body = changes(before, form);
  const invalid = !visible(form.title) || !validYear(form.year);

  async function save(event) {
    event.preventDefault();
    setSaved(false);
    if (await run(() => patch(`/api/materials/${encodeURIComponent(material.id)}`, body))) {
      setSaved(true);
      onSaved();
    }
  }

  const set = (name) => (event) => { setSaved(false); setForm((current) => ({ ...current, [name]: event.target.value })); };
  const field = (name, label, control) => (
    <div className="grid gap-1.5">
      <label htmlFor={`${id}-${name}`} className="text-xs font-medium text-muted-foreground">{label}</label>
      {control}
    </div>
  );
  return (
    <form onSubmit={save} className="grid gap-3" aria-label={t('paper.details')}>
      <h4 className="text-sm font-semibold">{t('paper.details')}</h4>
      {field('title', t('paper.field.title'), <Textarea id={`${id}-title`} rows={2} value={form.title} onChange={set('title')} />)}
      {field('authors', t('paper.field.authors'), <Textarea id={`${id}-authors`} rows={3} value={form.authors}
        placeholder={t('paper.field.authorsPlaceholder')} onChange={set('authors')} />)}
      <div className="grid gap-3 sm:grid-cols-[8rem_1fr]">
        {field('year', t('paper.field.year'), <Input id={`${id}-year`} inputMode="numeric" value={form.year} onChange={set('year')}
          aria-invalid={!validYear(form.year) || undefined} />)}
        {field('venue', t('paper.field.venue'), <Input id={`${id}-venue`} value={form.venue} onChange={set('venue')} />)}
      </div>
      {field('doi', t('paper.field.doi'), <Input id={`${id}-doi`} value={form.doi} spellCheck={false} className="font-mono text-xs md:text-xs"
        placeholder={t('paper.field.doiPlaceholder')} onChange={set('doi')} />)}
      {!visible(form.title) && <p className="text-xs text-destructive">{t('paper.titleNeeded')}</p>}
      {!validYear(form.year) && <p className="text-xs text-destructive">{t('paper.yearInvalid')}</p>}
      {problem && <p role="alert" className="text-sm text-destructive">{problem}</p>}
      {saved && <p role="status" className="text-sm text-success">{t('paper.saved')}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || invalid || !Object.keys(body).length}>{t('common.save')}</Button>
        <Button type="button" size="sm" variant="ghost" disabled={busy || !Object.keys(body).length}
          onClick={() => setForm(before)}>{t('common.cancel')}</Button>
      </div>
    </form>
  );
}

// The paper's text: a PDF's pages with their passages' boxes, or its passages in order. Each version
// has its own (keyed by it), and the view follows the version's kind: only a PDF has pages.
function Contents({ material }) {
  const t = useT();
  const pdf = isPdf(material);
  const [chosen, setChosen] = useState('pages');
  const view = viewOf(material, chosen);
  const [passages, setPassages] = useState(null);
  const pages = useMemo(() => passages && byPage(passages), [passages]); // once per set, not for each page shown
  const [failed, setFailed] = useState(false);
  const [pointed, setPointed] = useState(NOT_POINTED); // the passages with focus and under the pointer
  const [near, setNear] = useState(() => new Set()); // the pages within two screens of the view
  const onNear = useCallback((page, isNear) => setNear((current) => withNear(current, page, isNear)), []);
  const held = heldPages(near); // the pages holding their images: a bounded number, near the view
  const version = material.version.id;
  useEffect(() => {
    let live = true;
    setPassages(null);
    setPointed(NOT_POINTED); // their elements go: no blur or leave may come for them
    loadPassages(version).then((found) => live && setPassages(found)).catch(() => live && setFailed(true));
    return () => { live = false; };
  }, [version, material.extraction?.passages]);
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">{t('paper.text')}</h4>
        {pdf && <Segmented label={t('paper.view')} value={view} onChange={setChosen}
          options={[{ value: 'pages', label: t('paper.pages') }, { value: 'text', label: t('paper.passages') }]} />}
      </div>
      {failed && <p role="alert" className="text-sm text-destructive">{t('paper.loadFailed')}</p>}
      {!passages && !failed && <p role="status" className="text-sm text-muted-foreground">{t('common.loading')}</p>}
      {passages && view === 'pages' && (
        <div className="grid gap-4">
          {Array.from({ length: material.extraction.pages ?? 0 }, (_, i) => (
            <PageView key={i} version={version} number={i + 1} passages={pages.get(i + 1) ?? []}
              pointed={pointed} onPoint={setPointed} held={held.has(i + 1)} onNear={onNear} />
          ))}
        </div>
      )}
      {passages && view === 'text' && <PassageList passages={passages} pointed={pointed} onPoint={setPointed} />}
    </section>
  );
}

// The element that scrolls the page viewer: the root its pages are near or far from.
function scroller(element) {
  for (let node = element.parentElement; node; node = node.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY)) return node;
  }
  return null;
}

// One PDF page with the boxes of its passages over it: its image held only while held (near the
// view, heldPages), fetched when it is and let go when it is not, its shape kept meanwhile.
function PageView({ version, number, passages, pointed, onPoint, held, onNear }) {
  const t = useT();
  const frame = useRef(null);
  const [src, setSrc] = useState(null);
  const [failed, setFailed] = useState(false);
  const [aspect, setAspect] = useState(null); // its image's width over height, once one was shown
  useEffect(() => { // says whether it is within two screens of the view, for as long as it is shown
    const element = frame.current;
    if (!element) return undefined;
    if (typeof IntersectionObserver === 'undefined') { onNear(number, true); return () => onNear(number, false); }
    const observer = new IntersectionObserver((entries) => onNear(number, entries.some((entry) => entry.isIntersecting)),
      { root: scroller(element), rootMargin: '200% 0px' });
    observer.observe(element);
    return () => { observer.disconnect(); onNear(number, false); };
  }, [number, onNear]);
  useEffect(() => {
    if (!held) { setSrc(null); return undefined; } // let go: fetched again (no-store) once it is near
    if (src) return undefined;
    let live = true;
    pageImage(version, number).then((url) => live && setSrc(url)).catch(() => live && setFailed(true));
    return () => { live = false; };
  }, [held, src, version, number]);
  return (
    <figure ref={frame} className="relative mx-auto w-full max-w-[720px] overflow-hidden rounded-md border bg-white shadow-xs"
      aria-label={t('paper.page', { number })}>
      {src ? <img src={src} alt={t('paper.page', { number })} className="block w-full" draggable={false}
        onLoad={(event) => setAspect(event.currentTarget.naturalWidth / event.currentTarget.naturalHeight)} />
        : <div className={cn('grid place-items-center text-xs', !aspect && 'aspect-[612/792]', failed ? 'text-destructive' : 'text-muted-foreground')}
          style={aspect ? { aspectRatio: aspect } : undefined}>
          {failed ? t('paper.pageFailed') : t('common.loading')}
        </div>}
      {src && passages.map((passage) => (passage.boxes?.rects ?? []).map((rect, i) => (
        <span key={`${passage.id}:${i}`} aria-hidden="true" title={passage.text}
          {...hovering(passage.id, onPoint)}
          className={cn('absolute rounded-[2px] transition-colors duration-150',
            isPointed(pointed, passage.id) ? 'bg-brand/25 ring-1 ring-brand/60' : 'bg-brand/10 ring-1 ring-brand/20 hover:bg-brand/20')}
          style={rectStyle(rect)} />
      )))}
      {/* One focusable region per passage, around its lines: the keyboard's way to it, with the same highlight. */}
      {src && passages.filter((passage) => passage.boxes?.rects?.length).map((passage) => (
        <span key={passage.id} role="note" aria-label={passage.text} data-passage={passage.id}
          {...pointing(passage.id, onPoint)}
          className="pointer-events-none absolute rounded-[3px] outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          style={rectStyle(unionRect(passage.boxes.rects))} />
      ))}
      <figcaption className="absolute bottom-1 right-2 rounded bg-black/50 px-1.5 text-[11px] text-white">{number}</figcaption>
    </figure>
  );
}

const KIND_STYLES = {
  title: 'text-base font-semibold',
  abstract: 'text-sm leading-relaxed',
  table: 'whitespace-pre-wrap rounded-md bg-muted px-3 py-2 font-mono text-xs leading-relaxed',
  caption: 'text-sm italic text-muted-foreground',
  reference: 'text-xs leading-relaxed text-muted-foreground',
  paragraph: 'text-sm leading-relaxed',
};

// The passages in order, under their section headings, each with its kind and page.
function PassageList({ passages, pointed, onPoint }) {
  const t = useT();
  let section = null;
  return (
    <ol className="grid gap-3" aria-label={t('paper.passages')}>
      {passages.map((passage) => {
        const path = passage.section_path.join(' › ');
        const heading = path && path !== section ? path : null;
        section = path || section;
        return (
          <li key={passage.id} data-passage={passage.id} {...pointing(passage.id, onPoint)}
            className="grid gap-1.5 rounded-md outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
            {heading && <p className="pt-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{heading}</p>}
            <div className={cn('rounded-md px-2 py-1 transition-colors duration-150', isPointed(pointed, passage.id) && 'bg-brand-soft')}>
              {passage.kind !== 'paragraph' && (
                <span className="mr-2 text-[11px] font-medium uppercase tracking-wide text-brand">{t(`paper.kind.${passage.kind}`)}</span>
              )}
              {passage.page != null && <span className="mr-2 text-[11px] tabular-nums text-muted-foreground">{t('paper.onPage', { number: passage.page })}</span>}
              <p className={cn('break-words', KIND_STYLES[passage.kind])}>{passage.text}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
