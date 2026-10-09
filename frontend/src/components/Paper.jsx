// i18n: migrated
// A paper's page (S7): its details for editing, its state and what is known of it, and its text. A
// PDF's pages are images rendered by the backend (pypdfium2) with the passages' boxes drawn over
// them; every format also shows its passages as text, in order. The cited passage's highlight comes
// with S1-19's citations; here a passage is highlighted while it is pointed at or focused, and Tab
// reaches each passage, in the text and on the page.
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { ArrowLeft, FileUp, Trash2 } from 'lucide-react';
import { useT } from '../i18n/index.js';
import { patch } from '../api.js';
import { useAction } from '../action.js';
import { visible } from '../text.js';
import { ACCEPT, changes, detailsOf, headings, heldPages, hovering, isPdf, isPointed, libraryChanged, NOT_POINTED, pageImage,
  pageLines, pageOffsets, pagePart, pagesWithin, pageWindow, PAGE_WIDTH, partMove, passOn, PASSAGE_STRETCH, passageStretch, pointing,
  rectStyle, reasonKey, selectedParts, takeSaved, unionRect, validYear, viewOf, waitsOn, withFocus, withNear } from '../library.js';
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
  const views = useRef(null); // the view's switch, whose Passages focus goes to when a note opens that view
  const view = viewOf(material, chosen);
  const [pointed, setPointed] = useState(NOT_POINTED); // the passages with focus and under the pointer
  const version = material.version.id;
  const count = material.extraction.passages ?? 0;
  useEffect(() => setPointed(NOT_POINTED), [count]); // read again: its passages go, with no blur or leave for them
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">{t('paper.text')}</h4>
        {pdf && <div ref={views}><Segmented label={t('paper.view')} value={view} onChange={setChosen}
          options={[{ value: 'pages', label: t('paper.pages') }, { value: 'text', label: t('paper.passages') }]} /></div>}
      </div>
      {/* Read again (its count changes), its parts start anew and read the new reading's passages. */}
      {view === 'pages' ? <PageList key={count} version={version} pages={material.extraction.pages ?? 0}
        pointed={pointed} onPoint={setPointed}
        onText={() => { views.current?.querySelectorAll('[role="radio"]')[1]?.focus(); setChosen('text'); }}
        toSwitch={() => views.current?.querySelector('[role="radio"][aria-checked="true"]')?.focus()} />
        : <PassageList key={count} version={version} count={count} pointed={pointed} onPoint={setPointed} />}
    </section>
  );
}

// The parts of a list (list, a ref to its element) that hold what they show: those near the view,
// the one holding focus and the shown ones the reader's selection takes in (heldPages). Focus going
// from one part to another keeps the one it leaves until the next takes it (to: where it goes), so
// the part it goes to, held or mounted beside it, is never let go before focus arrives.
function useHeld(list) {
  const [near, setNear] = useState(() => new Set()); // the parts within two screens of the view
  const [within, setWithin] = useState(null); // the part holding focus
  const [selected, setSelected] = useState([]); // the shown parts the selection takes in
  const onNear = useCallback((part, isNear) => setNear((current) => withNear(current, part, isNear)), []);
  const onWithin = useCallback((part, inside, to) => {
    const toList = Boolean(to && list.current?.contains(to)); // read as focus moves, not when React runs the update
    setWithin((current) => withFocus(current, part, inside, toList));
  }, [list]);
  useEffect(() => {
    const changed = () => {
      const found = list.current ? selectedParts(document.getSelection(), [...list.current.querySelectorAll('[data-part][data-shown]')]) : [];
      setSelected((current) => (current.join() === found.join() ? current : found));
    };
    document.addEventListener('selectionchange', changed);
    return () => document.removeEventListener('selectionchange', changed);
  }, [list]);
  return { held: heldPages(near, [within, ...selected]), within, onNear, onWithin };
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
// next, and passTo(element) gives focus waiting so to element instead (Retry, once a read failed).
// Focus that leaves it meanwhile stays where it went. Returns its element's props, focusOn and passTo.
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
      if (!event.currentTarget.contains(event.relatedTarget)) onWithin(part, false, event.relatedTarget);
    },
  };
  const focusOn = (where) => { frame.current?.focus(); entering.current = where; }; // after onFocus, which sets it too
  const passTo = (element) => { if (waitsOn(frame.current, document.activeElement, entering.current)) element?.focus(); };
  return [props, focusOn, passTo];
}

// A PDF's pages, each holding its image and its passages only while it is held. Only the pages
// within two screens of the view are mounted, with the held ones and a page on each side of each
// (pageWindow); the others are spacers of their height, each page's from its own image once shown,
// so the list keeps its height and every page its place, however many pages there are.
// Each page also keeps the part of its passages it last showed (startPart), mounted again or not.
// Engines cap an element's height (16,777,214 px in some Chromium builds), so the list lays out the
// pages that fit in MAX_LIST_HEIGHT (pageOffsets: about 15,800 letter pages 720 px wide) and after the
// last says the later ones are not shown here, with a button to the text view, which holds their text.
// ponytail: a scaled or paged list if every page of longer PDFs must be shown as a page.
function PageList({ version, pages, pointed, onPoint, onText, toSwitch }) {
  const t = useT();
  const list = useRef(null);
  const { held, within, onNear, onWithin } = useHeld(list);
  const aspects = useRef(new Map()); // each page's width over its height, once its image was shown
  const [learned, learn] = useReducer((n) => n + 1, 0);
  const [width, setWidth] = useState(PAGE_WIDTH);
  const offsets = useMemo(() => pageOffsets(pages, width, aspects.current), [pages, width, learned]);
  const [span, setSpan] = useState([1, 1]); // the first and last pages within two screens of the view
  const measure = useRef(null);
  measure.current = () => {
    const element = list.current;
    const root = scroller(element);
    const view = root ? root.getBoundingClientRect() : { top: 0, bottom: window.innerHeight, height: window.innerHeight };
    const top = element.getBoundingClientRect().top;
    const next = pagesWithin(offsets, view.top - top - 2 * view.height, view.bottom - top + 2 * view.height);
    setSpan((current) => (current[0] === next[0] && current[1] === next[1] ? current : next));
  };
  useLayoutEffect(() => measure.current(), [offsets]);
  useEffect(() => {
    const element = list.current;
    const root = scroller(element) ?? window;
    const follow = () => measure.current();
    const resized = new ResizeObserver(() => { setWidth((current) => element.clientWidth || current); follow(); }); // hidden: as it was
    resized.observe(element);
    if (root !== window) resized.observe(root);
    root.addEventListener('scroll', follow, { passive: true });
    return () => { resized.disconnect(); root.removeEventListener('scroll', follow); };
  }, []);
  const onAspect = useCallback((number, aspect) => {
    if (!(aspect > 0 && Number.isFinite(aspect)) || aspects.current.get(number) === aspect) return;
    aspects.current.set(number, aspect);
    learn();
  }, []);
  const parts = useRef(new Map()); // the part of its passages each page last showed
  const onPart = useCallback((number, part) => parts.current.set(number, part), []);
  const shown = offsets.length - 1; // the pages laid out: all of them, or those under the cap
  const note = useRef(null);
  // The cut coming before the page holding focus (the list widened, or a taller page's shape was learned) takes
  // that page away, and focus with it: focus goes to the note, which says where its text is. Focus that had
  // already left the list stays where it went.
  useLayoutEffect(() => {
    if (within == null || within <= shown) return;
    const lost = !document.activeElement || document.activeElement === document.body;
    onWithin(within, false);
    if (lost) note.current?.focus();
  }, [within, shown]);
  // The note goes once every page fits again (the list narrowed), and focus on its button with it: focus goes to
  // the view's switch, on its current option, as Show passages leads there. Focus elsewhere stays there.
  const noteFocused = useRef(false);
  useLayoutEffect(() => {
    if (shown < pages || !noteFocused.current) return;
    noteFocused.current = false;
    if (!document.activeElement || document.activeElement === document.body) toSwitch();
  }, [shown]);
  return (
    <div ref={list} className="grid gap-4">
      {pageWindow(offsets, span, held).map((item) => (item.page
        ? <PageView key={item.page} version={version} number={item.page} pointed={pointed} onPoint={onPoint}
          held={held.has(item.page)} onNear={onNear} onWithin={onWithin} aspect={aspects.current.get(item.page)} onAspect={onAspect}
          startPart={parts.current.get(item.page) ?? 0} onPart={onPart} />
        : <div key={`before-${item.spacer}`} aria-hidden="true" style={{ height: item.height }} />))}
      {shown < pages && <div className="grid justify-items-center gap-2 py-4 text-center">
        <p className="text-sm text-muted-foreground">{t('paper.pagesCut', { number: shown })}</p>
        <Button ref={note} type="button" variant="outline" size="sm" onClick={onText}
          onFocus={() => { noteFocused.current = true; }} onBlur={() => { noteFocused.current = false; }}>
          {t('paper.showPassages')}</Button>
      </div>}
    </div>
  );
}

// One PDF page with the boxes of its passages over it: its image and a part of its passages
// (PAGE_PART, most pages have fewer) held only while held (near the view or holding focus,
// heldPages), fetched when it is and let go when it is not, its shape kept meanwhile. A page of more
// has a button to the part before and one to the part after, in Tab's order, each passing focus on
// to that part's passages; while that part loads, what the page shows is out of Tab's and the
// pointer's reach and Tab toward it (Shift+Tab toward the part before) waits on the page, and a
// read that fails shows beside the button, with Retry, which (as the button does) reads it again
// (partMove); a page whose first read fails says so in its place, with Retry. The line boxes a part
// draws are bounded (pageLines). Its shape is its image's (aspect, kept by the list: onAspect).
function PageView({ version, number, pointed, onPoint, held, onNear, onWithin, aspect, onAspect, startPart, onPart }) {
  const t = useT();
  const frame = useRef(null);
  const retry = useRef(null);
  const [part, setPart] = useState(startPart); // which part of its passages it shows
  const [shown, setShown] = useState(null); // { src, part, passages, more }
  const [failed, setFailed] = useState(false); // read again once asked to, or let go and held again
  const [props, focusOn, passTo] = usePart(frame, number, shown ? shown.part : null, onNear, onWithin);
  const move = partMove(shown, part, failed);
  useEffect(() => {
    // Let go: fetched again (no-store) once it is near, on the part it showed, not one still to come or failed.
    if (!held) { setPart(shown?.part ?? part); setShown(null); setFailed(false); return undefined; }
    if (!move.read) return undefined;
    let live = true;
    const controller = new AbortController(); // let go before they came: its requests go too
    Promise.all([shown?.src ?? pageImage(version, number, 1.5, controller.signal), pagePart(version, number, part, controller.signal)])
      .then(([src, found]) => { if (live) { setShown({ src, part, ...found }); onPart(number, part); } })
      .catch(() => live && setFailed(true));
    return () => { live = false; controller.abort(); };
  }, [held, shown, version, number, part, failed]);
  useEffect(() => { // focus waiting on the page for the part goes to Retry once its read failed
    if (move.failed) passTo(retry.current);
  }, [move.failed]);
  const lines = shown && pageLines(shown.passages);
  const go = (to, where) => { focusOn(where); setPart(to); setFailed(false); };
  const button = 'rounded bg-black/60 px-2 py-0.5 text-[11px] text-white outline-hidden focus-visible:ring-2 focus-visible:ring-ring';
  // A button to another part of its passages, and once that part's read failed, the failure and Retry beside it.
  const control = (to, where, label, edge) => (
    <div className={cn('absolute left-2 z-10 flex flex-col items-start gap-1', edge)}>
      <button type="button" className={button} onClick={() => go(to, where)}>{label}</button>
      {move.failed && to === part && <>
        <p role="alert" className="rounded bg-black/60 px-2 py-0.5 text-[11px] text-white">{t('paper.loadFailed')}</p>
        <button ref={retry} type="button" className={button} onClick={() => go(to, where)}>{t('common.retry')}</button>
      </>}
    </div>
  );
  return (
    <figure ref={frame} {...props}
      onKeyDown={(event) => { if (move.loading && event.key === 'Tab' && event.shiftKey === part < shown.part) event.preventDefault(); }}
      className="relative mx-auto w-full max-w-[720px] overflow-hidden rounded-md border bg-white shadow-xs outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
      aria-label={t('paper.page', { number })}>
      {shown ? <img src={shown.src} alt={t('paper.page', { number })} className="block w-full" draggable={false}
        onLoad={(event) => onAspect(number, event.currentTarget.naturalWidth / event.currentTarget.naturalHeight)} />
        : <div className={cn('grid place-items-center text-xs', !aspect && 'aspect-[612/792]', failed ? 'text-destructive' : 'text-muted-foreground')}
          style={aspect ? { aspectRatio: aspect } : undefined}>
          {move.failed ? <div className="grid justify-items-center gap-2">
            <p role="alert">{t('paper.pageFailed')}</p>
            <button ref={retry} type="button" className={button} onClick={() => go(part, 'first')}>{t('common.retry')}</button>
          </div> : t('common.loading')}
        </div>}
      <div className="contents" inert={move.loading}>
        {shown?.part > 0 && control(shown.part - 1, 'last', t('paper.earlierPassages'), 'top-2')}
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
        {shown?.more && control(shown.part + 1, 'first', t('paper.laterPassages'), 'bottom-2')}
      </div>
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
// (aria-posinset of aria-setsize), as only the stretches held are in the page. A read that fails
// says so with Retry, which reads it again, as a page's part does (partMove): focus waiting on the
// stretch goes to Retry, and from Retry back to the stretch, on to its first passage once it shows.
function PassageStretch({ version, index, count, pointed, onPoint, held, onNear, onWithin }) {
  const t = useT();
  const frame = useRef(null);
  const retry = useRef(null);
  const [shown, setShown] = useState(null); // { passages, before }
  const [failed, setFailed] = useState(false); // read again once asked to, or let go and held again
  const [height, setHeight] = useState(null); // as it was last shown
  const [part, focusOn, passTo] = usePart(frame, index, shown && index, onNear, onWithin);
  const move = partMove(shown && { part: index }, index, failed);
  const size = Math.min(PASSAGE_STRETCH, count - index * PASSAGE_STRETCH);
  useEffect(() => {
    if (!held) { setShown(null); setFailed(false); return undefined; }
    if (!move.read) return undefined;
    let live = true;
    const controller = new AbortController();
    passageStretch(version, index, controller.signal).then((found) => live && setShown(found)).catch(() => live && setFailed(true));
    return () => { live = false; controller.abort(); };
  }, [held, shown, failed, version, index]);
  useEffect(() => { // focus waiting on the stretch for its passages goes to Retry once its read failed
    if (move.failed) passTo(retry.current);
  }, [move.failed]);
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
      )) : move.failed ? <div className="sticky top-0 flex flex-wrap items-center gap-2 self-start"> {/* in view while its box is */}
        <p role="alert" className="text-sm text-destructive">{t('paper.loadFailed')}</p>
        <Button ref={retry} type="button" variant="outline" size="sm" onClick={() => { focusOn('first'); setFailed(false); }}>
          {t('common.retry')}</Button>
      </div> : <p className="text-sm text-muted-foreground">{t('common.loading')}</p>}
    </div>
  );
}
