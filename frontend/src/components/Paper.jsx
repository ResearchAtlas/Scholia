// i18n: migrated
// A paper's page (S7): its details for editing, its state and what is known of it, and its text. A
// PDF's pages are images rendered by the backend (pypdfium2) with the passages' boxes drawn over
// them; every format also shows its passages as text, in order. The cited passage's highlight comes
// with S1-19's citations; here a passage is highlighted while it is pointed at or focused, and Tab
// reaches each passage, in the text and on the page.
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { ArrowLeft, FileUp, Trash2 } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { patch } from '../api.js';
import { useAction } from '../action.js';
import { visible } from '../text.js';
import { ACCEPT, changes, detailsOf, headings, heldPages, hovering, isPdf, isPointed, libraryChanged, NOT_POINTED, pageImage,
  pageLines, pagePart, passOn, PASSAGE_STRETCH, passageStretch, pointing, rectStyle, reasonKey, selectedParts, takeSaved,
  unionRect, validYear, viewOf, withNear } from '../library.js';
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
      if (await addTo(project.id, [...files].slice(0, 1), t, setNotice, { materialId: material.id,
        replaces: material.version?.id })) onChanged(); // the version shown when the file was chosen
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
// has its own (keyed by it), and the view follows the version's kind: only a PDF has pages. Neither
// holds the whole text: each part (a page, a stretch of passages) reads what it shows as it comes near
// the view and lets it go as it leaves, and a part far from the view is an empty box of its size.
function Contents({ material }) {
  const t = useT();
  const pdf = isPdf(material);
  const [chosen, setChosen] = useState('pages');
  const view = viewOf(material, chosen);
  const [pointed, setPointed] = useState(NOT_POINTED); // the passages with focus and under the pointer
  const version = material.version.id;
  const count = material.extraction.passages ?? 0;
  useEffect(() => setPointed(NOT_POINTED), [count]); // read again: its passages go, with no blur or leave for them
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">{t('paper.text')}</h4>
        {pdf && <Segmented label={t('paper.view')} value={view} onChange={setChosen}
          options={[{ value: 'pages', label: t('paper.pages') }, { value: 'text', label: t('paper.passages') }]} />}
      </div>
      {/* Read again (its count changes), its parts start anew and read the new reading's passages. */}
      {view === 'pages' ? <PageList key={count} version={version} pages={material.extraction.pages ?? 0}
        pointed={pointed} onPoint={setPointed} />
        : <PassageList key={count} version={version} count={count} pointed={pointed} onPoint={setPointed} />}
    </section>
  );
}

// The parts of a list (list, a ref to its element) that hold what they show: those near the view,
// the one holding focus and the shown ones the reader's selection takes in (heldPages).
function useHeld(list) {
  const [near, setNear] = useState(() => new Set()); // the parts within two screens of the view
  const [within, setWithin] = useState(null); // the part holding focus
  const [selected, setSelected] = useState([]); // the shown parts the selection takes in
  const onNear = useCallback((part, isNear) => setNear((current) => withNear(current, part, isNear)), []);
  const onWithin = useCallback((part, inside) => setWithin((current) => (inside ? part : current === part ? null : current)), []);
  useEffect(() => {
    const changed = () => {
      const found = list.current ? selectedParts(document.getSelection(), [...list.current.querySelectorAll('[data-part][data-shown]')]) : [];
      setSelected((current) => (current.join() === found.join() ? current : found));
    };
    document.addEventListener('selectionchange', changed);
    return () => document.removeEventListener('selectionchange', changed);
  }, [list]);
  return { held: heldPages(near, [within, ...selected]), onNear, onWithin };
}

// The element that scrolls the paper's text: the root its parts are near or far from.
function scroller(element) {
  for (let node = element.parentElement; node; node = node.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY)) return node;
  }
  return null;
}

// A part of the text (a page, a stretch of passages), showing what showing names (null while it
// shows nothing): says whether it is within two screens of the view for as long as it is shown, and
// whether focus is in it. While its passages are not shown it takes Tab's focus in their place; once
// they show, it passes focus on to the first of them, or to the last when focus came back from after
// it (passOn), so Tab reaches every passage in order. focusOn(where) asks the same of what it shows
// next. Focus that leaves it meanwhile stays where it went. Returns its element's props and focusOn.
function usePart(frame, part, showing, onNear, onWithin) {
  const entering = useRef(null);
  const shown = showing != null;
  useEffect(() => {
    const element = frame.current;
    if (!element) return undefined;
    if (typeof IntersectionObserver === 'undefined') { onNear(part, true); return () => onNear(part, false); }
    const observer = new IntersectionObserver((entries) => onNear(part, entries.some((entry) => entry.isIntersecting)),
      { root: scroller(element), rootMargin: '200% 0px' });
    observer.observe(element);
    return () => { observer.disconnect(); onNear(part, false); };
  }, [frame, part, onNear]);
  useEffect(() => {
    if (!shown) return;
    passOn(frame.current, document.activeElement, entering.current)?.focus();
    entering.current = null;
  }, [frame, shown, showing]);
  const props = {
    tabIndex: shown ? -1 : 0, // shown, it keeps focus given to it until its passages take it, out of Tab's way
    'data-part': part,
    'data-shown': shown ? '' : undefined,
    onFocus: (event) => {
      onWithin(part, true);
      if (shown || event.target !== event.currentTarget) return;
      const from = event.relatedTarget;
      entering.current = from && event.currentTarget.compareDocumentPosition(from) & Node.DOCUMENT_POSITION_FOLLOWING
        ? 'last' : 'first';
    },
    onBlur: (event) => {
      if (event.target === event.currentTarget) entering.current = null; // left before its passages came
      if (!event.currentTarget.contains(event.relatedTarget)) onWithin(part, false);
    },
  };
  const focusOn = (where) => { entering.current = where; frame.current?.focus(); };
  return [props, focusOn];
}

// A PDF's pages, each holding its image and its passages only while it is held.
function PageList({ version, pages, pointed, onPoint }) {
  const list = useRef(null);
  const { held, onNear, onWithin } = useHeld(list);
  return (
    <div ref={list} className="grid gap-4">
      {Array.from({ length: pages }, (_, i) => (
        <PageView key={i} version={version} number={i + 1} pointed={pointed} onPoint={onPoint}
          held={held.has(i + 1)} onNear={onNear} onWithin={onWithin} />
      ))}
    </div>
  );
}

// One PDF page with the boxes of its passages over it: its image and a part of its passages
// (PAGE_PART, most pages have fewer) held only while held (near the view or holding focus,
// heldPages), fetched when it is and let go when it is not, its shape kept meanwhile. A page of more
// has a button to the part before and one to the part after, in Tab's order, each passing focus on
// to that part's passages; the line boxes a part draws are bounded (pageLines).
function PageView({ version, number, pointed, onPoint, held, onNear, onWithin }) {
  const t = useT();
  const frame = useRef(null);
  const [part, setPart] = useState(0); // which part of its passages it shows
  const [shown, setShown] = useState(null); // { src, part, passages, more }
  const [failed, setFailed] = useState(false);
  const [aspect, setAspect] = useState(null); // its image's width over height, once one was shown
  const [props, focusOn] = usePart(frame, number, shown ? shown.part : null, onNear, onWithin);
  useEffect(() => {
    if (!held) { setShown(null); return undefined; } // let go: fetched again (no-store) once it is near
    if (shown?.part === part) return undefined;
    let live = true;
    const controller = new AbortController(); // let go before they came: its requests go too
    Promise.all([shown?.src ?? pageImage(version, number, 1.5, controller.signal), pagePart(version, number, part, controller.signal)])
      .then(([src, found]) => live && setShown({ src, part, ...found })).catch(() => live && setFailed(true));
    return () => { live = false; controller.abort(); };
  }, [held, shown, version, number, part]);
  const lines = shown && pageLines(shown.passages);
  const go = (to, where) => { focusOn(where); setPart(to); };
  const button = 'absolute left-2 z-10 rounded bg-black/60 px-2 py-0.5 text-[11px] text-white outline-hidden focus-visible:ring-2 focus-visible:ring-ring';
  return (
    <figure ref={frame} {...props}
      className="relative mx-auto w-full max-w-[720px] overflow-hidden rounded-md border bg-white shadow-xs outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
      aria-label={t('paper.page', { number })}>
      {shown ? <img src={shown.src} alt={t('paper.page', { number })} className="block w-full" draggable={false}
        onLoad={(event) => setAspect(event.currentTarget.naturalWidth / event.currentTarget.naturalHeight)} />
        : <div className={cn('grid place-items-center text-xs', !aspect && 'aspect-[612/792]', failed ? 'text-destructive' : 'text-muted-foreground')}
          style={aspect ? { aspectRatio: aspect } : undefined}>
          {failed ? t('paper.pageFailed') : t('common.loading')}
        </div>}
      {shown?.part > 0 && <button type="button" className={cn(button, 'top-2')}
        onClick={() => go(shown.part - 1, 'last')}>{t('paper.earlierPassages')}</button>}
      {shown?.passages.map((passage, n) => (lines[n] ?? []).map((rect, i) => (
        <span key={`${passage.id}:${i}`} aria-hidden="true" title={passage.text}
          {...hovering(passage.id, onPoint)}
          className={cn('absolute rounded-[2px] transition-colors duration-150',
            isPointed(pointed, passage.id) ? 'bg-brand/25 ring-1 ring-brand/60' : 'bg-brand/10 ring-1 ring-brand/20 hover:bg-brand/20')}
          style={rectStyle(rect)} />
      )))}
      {/* One focusable region per passage, around its lines: the keyboard's way to it, with the same
          highlight; the pointer's too, for a passage whose lines are not drawn. */}
      {shown?.passages.map((passage, n) => passage.boxes?.rects?.length > 0 && (
        <span key={passage.id} role="note" aria-label={passage.text} data-passage={passage.id}
          {...pointing(passage.id, onPoint)} title={lines[n] ? undefined : passage.text}
          className={cn('absolute rounded-[3px] outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
            lines[n] ? 'pointer-events-none' : isPointed(pointed, passage.id) ? 'bg-brand/25 ring-1 ring-brand/60'
              : 'bg-brand/10 ring-1 ring-brand/20 hover:bg-brand/20')}
          style={rectStyle(unionRect(passage.boxes.rects))} />
      ))}
      {shown?.more && <button type="button" className={cn(button, 'bottom-2')}
        onClick={() => go(shown.part + 1, 'first')}>{t('paper.laterPassages')}</button>}
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

// The passages in order, under their section headings, each with its kind and page: a stretch of
// PASSAGE_STRETCH at a time, each holding its passages only while it is held.
function PassageList({ version, count, pointed, onPoint }) {
  const t = useT();
  const list = useRef(null);
  const { held, onNear, onWithin } = useHeld(list);
  return (
    <div ref={list} role="list" className="grid gap-3" aria-label={t('paper.passages')}>
      {Array.from({ length: Math.ceil(count / PASSAGE_STRETCH) }, (_, i) => (
        <PassageStretch key={i} version={version} index={i} count={count}
          pointed={pointed} onPoint={onPoint} held={held.has(i)} onNear={onNear} onWithin={onWithin} />
      ))}
    </div>
  );
}

// One stretch of the passages: read when it is held and let go when it is not, its height kept
// meanwhile (estimated until it was first shown). Each passage says where it is in the whole text
// (aria-posinset of aria-setsize), as only the stretches held are in the page.
function PassageStretch({ version, index, count, pointed, onPoint, held, onNear, onWithin }) {
  const t = useT();
  const frame = useRef(null);
  const [shown, setShown] = useState(null); // { passages, before }
  const [failed, setFailed] = useState(false);
  const [height, setHeight] = useState(null); // as it was last shown
  const [part] = usePart(frame, index, shown && index, onNear, onWithin);
  const size = Math.min(PASSAGE_STRETCH, count - index * PASSAGE_STRETCH);
  useEffect(() => {
    if (!held) { setShown(null); return undefined; }
    if (shown) return undefined;
    let live = true;
    const controller = new AbortController();
    passageStretch(version, index, controller.signal).then((found) => live && setShown(found)).catch(() => live && setFailed(true));
    return () => { live = false; controller.abort(); };
  }, [held, shown, version, index]);
  useLayoutEffect(() => { if (shown) setHeight(frame.current.offsetHeight); }, [shown]);
  const over = shown && headings(shown.passages, shown.before);
  return (
    <div ref={frame} {...part} className="grid gap-3 rounded-md outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
      style={shown ? undefined : { height: height ?? size * 64 }}>
      {shown ? shown.passages.map((passage, i) => (
        <div role="listitem" key={passage.id} data-passage={passage.id} {...pointing(passage.id, onPoint)}
          aria-posinset={passage.ordinal + 1} aria-setsize={count}
          className="grid gap-1.5 rounded-md outline-hidden focus-visible:ring-2 focus-visible:ring-ring">
          {over[i] && <p className="pt-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{over[i]}</p>}
          <div className={cn('rounded-md px-2 py-1 transition-colors duration-150', isPointed(pointed, passage.id) && 'bg-brand-soft')}>
            {passage.kind !== 'paragraph' && (
              <span className="mr-2 text-[11px] font-medium uppercase tracking-wide text-brand">{t(`paper.kind.${passage.kind}`)}</span>
            )}
            {passage.page != null && <span className="mr-2 text-[11px] tabular-nums text-muted-foreground">{t('paper.onPage', { number: passage.page })}</span>}
            <p className={cn('break-words', KIND_STYLES[passage.kind])}>{passage.text}</p>
          </div>
        </div>
      )) : <p className={cn('text-sm', failed ? 'text-destructive' : 'text-muted-foreground')}>
        {failed ? t('paper.loadFailed') : t('common.loading')}</p>}
    </div>
  );
}
