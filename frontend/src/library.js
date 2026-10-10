// The Library (slice-1 spec S7, F3a) as the interface reads and sends it: which files Scholia reads,
// how a paper's state and reason are named, its details as the edit form holds them, and where a
// passage's boxes sit on its page image (backend/materials.py, backend/extraction.py).
import { ApiError, get, getBlob, post } from './api.js';
import { errorText } from './text.js';

export const ACCEPT = '.pdf,.docx,.html,.htm,.xhtml,.md,.markdown,.tex,.latex';
const SUPPORTED = new Set(ACCEPT.split(','));
export const MAX_FILES = 20; // per request (backend/materials.py MAX_FILES); a drop may hold any number
export const MAX_FILE_BYTES = 100 * 1024 * 1024; // per file (backend/extraction.py MAX_FILE_BYTES)
// The files' base64 a request may carry: one largest file (backend/local_guard.py MAX_BODY adds 64 KiB
// for the JSON around it).
export const REQUEST_FILE_BYTES = Math.ceil(MAX_FILE_BYTES / 3) * 4;

// Whether Scholia reads a file of this name: PDF, DOCX, HTML, Markdown or LaTeX source.
export function supported(name) {
  const dot = name.lastIndexOf('.');
  return dot > 0 && SUPPORTED.has(name.slice(dot).toLowerCase());
}

// The files of a drop or a choice Scholia reads, and the names of those it does not.
export function sortFiles(files) {
  const list = [...files];
  return { kept: list.filter((file) => supported(file.name)), skipped: list.filter((file) => !supported(file.name)).map((f) => f.name) };
}

// A file as the upload sends it: its name and its bytes in base64 (the API takes JSON only).
export function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, data: String(reader.result).split(',', 2)[1] ?? '' });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// The files in their order, in requests the backend takes: as many to a request as fit in
// REQUEST_FILE_BYTES of base64 (and MAX_FILES), so a selection that fits is one request, one batch.
export function requestsOf(files) {
  const groups = [];
  let size = Infinity;
  for (const file of files) {
    const encoded = Math.ceil(file.size / 3) * 4;
    if (size + encoded > REQUEST_FILE_BYTES || groups.at(-1).length >= MAX_FILES) {
      groups.push([]);
      size = 0;
    }
    groups.at(-1).push(file);
    size += encoded;
  }
  return groups;
}

// Adds files to a project: from a drop, Add files, or the conversation (conversationId), or as a
// new version of a material (materialId, replacing the version named by replaces: the backend refuses
// it once another has), however many. A file larger than Scholia reads refuses
// the selection, as the backend would; the rest go in as few requests as fit (requestsOf), read
// one request at a time, and stay one batch, which the backend holds in the drop's lookup run: each
// request but the last says more follow (more), the ones after the first name the batch the first
// answered (batch), and the last closes it, so a Local only project asks once, counting every
// identifier the drop gave. A batch the window never closes (it went away, or a request failed)
// closes itself on the backend. Resolves to the backend's answers together; a request that fails
// once others were added ends the sending, its error code in `problem`. Uploads run one after
// another, wherever they start (a drop, Add files, the conversation, Replace file): each reads its
// files' data only once it is its turn, so several started at once never hold theirs together. One
// that fails does not stop the next. uploadsWaiting() counts those started and not yet finished;
// watchUploads(listener) hears each change.
let uploads = Promise.resolve();
let waiting = 0;
export const uploadsWaiting = () => waiting;
export function watchUploads(listener) {
  libraryEvents.addEventListener('uploads', listener);
  return () => libraryEvents.removeEventListener('uploads', listener);
}
function waitingChanged(change) {
  waiting += change;
  libraryEvents.dispatchEvent(new Event('uploads'));
}
export function addFiles(projectId, files, options = {}) {
  waitingChanged(1);
  const turn = uploads.then(() => upload(projectId, files, options));
  uploads = turn.catch(() => null).then(() => waitingChanged(-1));
  return turn;
}

async function upload(projectId, files, { conversationId, materialId, replaces } = {}) {
  if (files.some((file) => file.size > MAX_FILE_BYTES)) throw new ApiError(413, 'file_too_large');
  const send = (body) => post(`/api/projects/${encodeURIComponent(projectId)}/materials`, {
    ...body, ...(conversationId ? { conversation_id: conversationId } : {}),
    ...(materialId ? { material_id: materialId, replaces } : {}),
  });
  const groups = requestsOf(files);
  const added = { materials: [], lookup_run_id: null };
  for (const [i, group] of groups.entries()) {
    const batch = added.lookup_run_id ? { batch: added.lookup_run_id } : {};
    try {
      // Every file read, or failed, before the next upload's turn: a failure does not end it while others still read.
      const read = await Promise.allSettled(group.map(readFile));
      const failed = read.find((r) => r.status === 'rejected');
      if (failed) throw failed.reason;
      const answer = await send({ files: read.map((r) => r.value), ...batch,
        ...(i < groups.length - 1 ? { more: true } : {}) });
      added.materials.push(...answer.materials);
      added.lookup_run_id = answer.lookup_run_id ?? added.lookup_run_id;
    } catch (error) {
      if (!added.materials.length) throw error;
      if (added.lookup_run_id) await send({ files: [], batch: added.lookup_run_id }).catch(() => null); // close it now
      return { ...added, problem: error instanceof ApiError ? error.code : 'internal' };
    }
  }
  return added;
}

// Tells the open Library that a project's papers changed (files added by a drop on the window).
export const libraryEvents = new EventTarget();
export const libraryChanged = (projectId) => libraryEvents.dispatchEvent(new CustomEvent('changed', { detail: projectId }));

// A file's type as a catalog key.
const TYPES = { 'application/pdf': 'pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'text/html': 'html', 'text/markdown': 'markdown', 'application/x-tex': 'latex' };
export const typeKey = (mediaType) => `library.type.${TYPES[mediaType] ?? 'other'}`;
export const isPdf = (material) => material?.version?.media_type === 'application/pdf';

// What a paper's page shows of its text: its pages when it is a PDF and they are chosen, else its
// passages as text (whatever was chosen for an earlier version that was a PDF).
export const viewOf = (material, chosen) => (isPdf(material) && chosen === 'pages' ? 'pages' : 'text');

// A stopped lookup's line in a paper's details, by what it recorded, as runOutcome reads a stopped run
// for the background-run list: its project changed (revoked), the researcher chose not to on its
// question (declined), or it was stopped (Cancel).
export const cancelledKey = (lookup) => (lookup.cancel_reason === 'revoked' ? 'library.source.projectChanged'
  : lookup.reason === 'declined' ? 'library.source.declined' : 'runs.stopped');

// A paper's state (reading, ready, needs_attention) and its reason, as catalog keys.
export function stateKey(material) {
  return `library.state.${material.state}`;
}

// A finished lookup's outcome for a paper, each with its text (library.source.*): what its details say.
export const LOOKUP_OUTCOMES = ['no_identifier', 'not_read', 'not_found', 'unavailable', 'refused'];

// The service a lookup took a paper's details from, by the key of their record (backend/lookup.py
// Found.source_key): an OpenAlex work, a DOI's Crossref record or an arXiv ID.
const SERVICES = { openalex: 'openalex', doi: 'crossref', arxiv: 'arxiv' };

// Where a paper's details come from, as text: the researcher's edit; the record a lookup took them
// from, and when (kept as they are whatever a later lookup does); or else the file, with what its
// latest lookup did.
export function detailsSource(t, material, project, date) {
  if (material.checked_by === 'researcher') return t('library.source.edited', { date: date(material.checked_at) });
  const service = SERVICES[material.source_key?.split(':')[0]];
  if (material.checked_by === 'lookup' && service) {
    return t('library.source.lookedUp', { source: t(`ask.service.${service}`), date: date(material.resolved_at) });
  }
  const lookup = material.lookup;
  if (!lookup) return t(project?.review_lock ? 'library.source.locked' : 'library.source.fromFile');
  if (lookup.status === 'running') return t(lookup.waiting ? 'library.source.waiting' : 'library.source.lookingUp');
  if (lookup.status === 'cancelled') return t(cancelledKey(lookup));
  const outcome = lookup.outcome;
  return t(LOOKUP_OUTCOMES.includes(outcome) ? `library.source.${outcome}` : 'library.source.fromFile');
}

// What a paper's latest lookup did, as text, beside details that came from elsewhere (an edit or an
// earlier lookup's record), or beside the file's when it ended with no outcome for the paper (failed,
// with its recorded reason, or interrupted); null when the details say it already, or when it has
// nothing to tell (it found their record, or its file was replaced meanwhile). Only a running lookup
// waits for an answer: one that ended while it waited reads as how it ended.
export function latestLookup(t, material) {
  const lookup = material.lookup;
  if (!lookup) return null;
  const ended = lookup.outcome != null ? null : lookup.status === 'failed' ? errorText(t, lookup.reason ?? 'internal')
    : lookup.status === 'interrupted' ? t('runs.interrupted') : null;
  if (!material.checked_by) return ended;
  if (lookup.status === 'running') return t(lookup.waiting ? 'library.source.waiting' : 'library.source.lookingUp');
  if (lookup.status === 'cancelled') return t(cancelledKey(lookup));
  return LOOKUP_OUTCOMES.includes(lookup.outcome) ? t(`library.lookup.${lookup.outcome}`) : ended;
}

const REASONS = new Set(['ocr_waiting', 'no_text', 'not_read', 'stopped', 'time_limit', 'unreadable_file',
  'encrypted_file', 'file_missing', 'interrupted', 'not_found', 'outdated', 'ocr_failed', 'memory_limit', 'step_limit']);

// A reading's scanned pages under Details: read by text recognition, or waiting for it where no OCR
// engine read them (status ocr_needed); null when it has none.
export function ocrPages(t, extraction) {
  if (!(extraction?.ocr_pages > 0)) return null;
  return t(extraction.status === 'ocr_needed' ? 'library.ocrWaiting' : 'library.ocrRead', { count: extraction.ocr_pages });
}

export function reasonKey(reason) {
  return reason ? `library.reason.${REASONS.has(reason) ? reason : 'other'}` : null;
}

// Whether the Library should look again soon: a paper is being read, a lookup runs, or an ask waits.
export function unsettled(listing) {
  return Boolean(listing && (listing.asks?.length
    || listing.materials?.some((m) => m.state === 'reading' || m.lookup?.status === 'running')));
}

// Authors, one per line, as the form shows them: "Family, Given", or a name as it was given.
export function authorLines(csl) {
  return (csl?.author ?? []).map((a) => (a.family ? (a.given ? `${a.family}, ${a.given}` : a.family) : a.literal ?? ''))
    .filter(Boolean).join('\n');
}

// Authors as a short line: up to three names, then "et al." handled by the caller's catalog.
export function authorNames(csl) {
  return (csl?.author ?? []).map((a) => (a.family ? [a.given, a.family].filter(Boolean).join(' ') : a.literal ?? ''))
    .filter(Boolean);
}

export function yearOf(csl) {
  const year = csl?.issued?.['date-parts']?.[0]?.[0];
  return Number.isInteger(year) ? year : null;
}

// The edit form's fields from a material, and the PATCH body from the form (only what changed).
export function detailsOf(material) {
  const csl = material?.csl ?? {};
  return { title: material?.title ?? '', authors: authorLines(csl), year: yearOf(csl)?.toString() ?? '',
    venue: csl['container-title'] ?? '', doi: csl.DOI ?? '' };
}

export function changes(before, after) {
  const body = {};
  if (after.title !== before.title) body.title = after.title;
  if (after.authors !== before.authors) body.authors = after.authors.split('\n').map((line) => line.trim()).filter(Boolean);
  if (after.year !== before.year) {
    const year = after.year.trim();
    body.year = year === '' ? null : Number(year);
  }
  if (after.venue !== before.venue) body.venue = after.venue;
  if (after.doi !== before.doi) body.doi = after.doi.trim();
  return body;
}

// The edit form once the saved details changed (a lookup, or this form's own save): the newly saved
// values, but each field the researcher changed since the form last took the saved ones kept as
// typed. Only those then differ from what is saved, so a save sends only them.
export function refreshed(shown, form, saved) {
  return Object.fromEntries(Object.keys(saved).map((name) => [name, form[name] !== shown[name] ? form[name] : saved[name]]));
}

// The form's effect once the saved details changed. The saved details it last took are read here,
// before the update is queued: React may run the update later, when shown.current already holds
// the new ones, and then every field left as it was would look edited and stay stale.
export function takeSaved(shown, saved, setForm) {
  const before = shown.current;
  shown.current = saved;
  setForm((form) => refreshed(before, form, saved));
}

// A year the form may send: empty, or a whole number from 1000 to 2200 (the backend's range).
export function validYear(text) {
  const year = text.trim();
  return year === '' || (/^\d{4}$/.test(year) && Number(year) >= 1000 && Number(year) <= 2200);
}

// A passage's line rectangle [left, top, right, bottom], fractions of its page from the top left,
// as the style that places it over the page image.
export function rectStyle([left, top, right, bottom]) {
  const pct = (value) => `${Math.round(value * 10000) / 100}%`;
  return { left: pct(left), top: pct(top), width: pct(Math.max(0, right - left)), height: pct(Math.max(0, bottom - top)) };
}

// The rectangle around all of a passage's line rectangles: where its one focusable region sits on
// its page, so the keyboard reaches the passage once however many lines it has.
export function unionRect(rects) {
  return [Math.min(...rects.map((r) => r[0])), Math.min(...rects.map((r) => r[1])),
    Math.max(...rects.map((r) => r[2])), Math.max(...rects.map((r) => r[3]))];
}

// Which passages are highlighted (in the text and on its page alike): the one with focus and the one
// under the pointer, kept apart and each highlighted. So the pointer never takes the highlight from
// the passage with focus (a scroll can bring another under a resting pointer, and its leaving would
// clear it), and Tab always shows where focus is, by its ring and its highlight. Each is cleared only
// by its own passage's leave or blur, so a late leave never clears the passage now pointed at.
export const NOT_POINTED = { focused: null, hovered: null };

export const isPointed = (state, id) => state.focused === id || state.hovered === id;

function point(onPoint, which, id, on) {
  return () => onPoint((state) => (on ? { ...state, [which]: id }
    : state[which] === id ? { ...state, [which]: null } : state));
}

// A passage's props for the pointer (each of its line boxes on a page). onPoint is the state's setter.
export function hovering(id, onPoint) {
  return { onMouseEnter: point(onPoint, 'hovered', id, true), onMouseLeave: point(onPoint, 'hovered', id, false) };
}

// A passage's props for pointing at it, by the pointer or by focus, and for Tab to reach it.
export function pointing(id, onPoint) {
  return { tabIndex: 0, ...hovering(id, onPoint),
    onFocus: point(onPoint, 'focused', id, true), onBlur: point(onPoint, 'focused', id, false) };
}

// Reads of which only the newest counts: asked() starts one and returns whether it is still the
// newest, so an answer that comes after a later read was asked (another project's, once the Library
// shows that one, or the next poll) is dropped and never replaces what is shown.
export function newest() {
  let count = 0;
  return () => {
    const mine = ++count;
    return () => mine === count;
  };
}

// Tells whichever view shows a conversation that files attached in it were added, so the question
// their lookup may ask can be there: that view reads its questions again, whichever instance it is
// (Shell rebuilds a conversation's view when a draft becomes the window's, while an upload may run).
export const asksEvents = new EventTarget();
export const asksChanged = (conversationId) => asksEvents.dispatchEvent(new CustomEvent('changed', { detail: conversationId }));

// A view's following of its conversation's changes: load() on each; returns the way to stop.
export function followAsks(conversationId, load) {
  const changed = (event) => { if (conversationId && event.detail === conversationId) load(); };
  asksEvents.addEventListener('changed', changed);
  return () => asksEvents.removeEventListener('changed', changed);
}

// What a conversation's view knows of its questions (useConversationAsks): the open ones, whether work
// started there still runs, and whether its last read failed, so that it has not learned whether
// work remains. A read's answer replaces it; a failed read (null) keeps what was shown and says so.
export const NO_ASKS = { asks: [], working: false, unsure: false };
export const afterRead = (known, found) => (found ? { asks: found.asks, working: found.working > 0, unsure: false }
  : { ...known, unsure: true });

// Whether the view reads its questions again: while its own attached files' lookup is watched, work
// runs, a question is open, or its last read failed (a wake-up's included), until a read succeeds.
export const pollsAsks = (known, watching) => Boolean(watching || known.working || known.asks.length || known.unsure);

// One read of a conversation's open questions (ConversationView's useConversationAsks): show(found)
// gets its answer, or null when the read failed, only while it is the newest read asked (newest()),
// so an answer for a conversation left since, or overtaken by a later read, never shows there.
export async function readAsks(asked, conversationId, show) {
  const current = asked();
  if (!conversationId) return;
  let found = null;
  try {
    found = await get(`/api/asks?conversation_id=${encodeURIComponent(conversationId)}`);
  } catch {
    // read again at the next turn
  }
  if (current()) show(found);
}

// The text view shows a version's passages a stretch at a time, each read when it comes near the view
// and let go when it leaves (heldPages): the stretch's passages and the section the passages before
// it end in (the API's section: the last path before it that is not empty, as a title's is), so its
// first heading shows as it does in the whole text.
export const PASSAGE_STRETCH = 100;
export async function passageStretch(versionId, index, signal) {
  const found = await get(`/api/material-versions/${encodeURIComponent(versionId)}/passages`
    + `?offset=${index * PASSAGE_STRETCH}&limit=${PASSAGE_STRETCH}`, { signal });
  return { passages: found.passages, before: found.section?.join(' › ') || null };
}

// The section heading over each passage: its section's path where that differs from the section
// before it (`before` for the first one), or null.
export function headings(passages, before = null) {
  let section = before;
  return passages.map((passage) => {
    const path = passage.section_path.join(' › ');
    const heading = path && path !== section ? path : null;
    section = path || section;
    return heading;
  });
}

// A PDF page shows its passages PAGE_PART at a time (most pages have fewer): the part asked for, and
// whether more follow it on the page. Each part draws the line boxes of its passages up to
// PAGE_LINES of them (pageLines); a passage past that is drawn as the one box around its lines.
export const PAGE_PART = 200;
export const PAGE_LINES = 2000;
export async function pagePart(versionId, number, part, signal) {
  const found = await get(`/api/material-versions/${encodeURIComponent(versionId)}/passages?page=${number}`
    + `&offset=${part * PAGE_PART}&limit=${PAGE_PART + 1}`, { signal });
  return { passages: found.passages.slice(0, PAGE_PART), more: found.passages.length > PAGE_PART };
}

// A PDF page and the part of its passages asked for (part), against the part it shows (shown, or
// null): read it unless it shows already or its read failed, until it is asked for again; loading
// while it is read in place of another, what the page shows then out of Tab's and the pointer's
// reach and Tab toward it waiting on the page, so focus stays there for passOn; failed once that
// read failed, with Retry: beside the button to the other part, the page showing its part as
// before, or in the page's place when it showed nothing yet. A stretch of the text view is a part
// of its own (shown { part: its index } or null).
export function partMove(shown, part, failed) {
  const moving = shown != null && shown.part !== part;
  return { read: shown?.part !== part && !failed, loading: moving && !failed, failed: shown?.part !== part && failed };
}

// Each passage's line boxes to draw, in order, while they fit in budget; null for each from the
// first that does not fit on.
export function pageLines(passages, budget = PAGE_LINES) {
  let left = budget;
  return passages.map((passage) => {
    const rects = passage.boxes?.rects ?? [];
    if (rects.length > left) left = -1;
    if (left < 0) return null;
    left -= rects.length;
    return rects;
  });
}

// The parts of a paper's text that hold what they show (a PDF's pages their images and passages, the
// text view's stretches their passages): those near the view (within two screens of the panel's
// scrolled view), at most MAX_HELD_PAGES, the ones around the middle of that stretch first, and the
// kept ones wherever they are: the one holding focus, so focus never goes with its passage, and
// those the reader's selection takes in (selectedParts), so a copy never leaves passages out. Any
// other part lets what it holds go, and fetches it again (no-store) once it comes near.
export const MAX_HELD_PAGES = 8;
export function heldPages(near, kept = [], limit = MAX_HELD_PAGES) {
  const pages = [...near].sort((a, b) => a - b);
  const first = Math.max(0, Math.floor((pages.length - limit) / 2));
  const held = new Set(pages.slice(first, first + limit));
  for (const part of kept) if (part != null) held.add(part);
  return held;
}

// The parts among those shown (elements with their data-part) that the selection takes in, wholly
// or in part.
export function selectedParts(selection, shown) {
  if (!selection?.rangeCount || selection.isCollapsed) return [];
  return shown.filter((element) => selection.containsNode(element, true)).map((element) => Number(element.dataset.part));
}

// Whether focus is on a part waiting for what it was asked to show (entering): not once focus has
// left the part, as entering is cleared then, and focus is checked to be on it still.
export function waitsOn(part, active, entering) {
  return Boolean(entering && part && active === part);
}

// Where focus goes once a part shows what it was asked for (its passages read, or another part of a
// page's), when focus was on the part itself waiting for them (waitsOn): its first passage, or its
// last when it came back from after it.
export function passOn(part, active, entering) {
  if (!waitsOn(part, active, entering)) return null;
  const passages = part.querySelectorAll('[data-passage]');
  return (entering === 'last' ? passages[passages.length - 1] : passages[0]) ?? null;
}

// A PDF's pages as the page viewer lays them out: each as wide as the list (at most PAGE_WIDTH), its
// height from its width over its height (its image's once shown, else the default page's), with its
// border's two pixels, and PAGE_GAP between pages.
export const PAGE_WIDTH = 720;
export const PAGE_GAP = 16;
export const PAGE_ASPECT = 612 / 792;

// The tallest the page list is laid out: under the lowest height an engine lays an element out to
// (16,777,214 px in some Chromium builds, 33,554,432 px in the app's WebKit), with room to spare.
export const MAX_LIST_HEIGHT = 15_000_000;

// Where each page begins in the list, from its top: offsets[n - 1] for page n, and offsets[length - 1]
// the list's height and a gap, for the pages that fit within most (the first at least): the list
// shows those, and says the others are in the text view. aspects holds the known ones, by page number.
// ponytail: rebuilt whole for each aspect learned (a millisecond for 100,000 pages); a Fenwick tree if
// documents of millions of pages are read.
export function pageOffsets(pages, width, aspects, most = MAX_LIST_HEIGHT) {
  const offsets = new Float64Array(pages + 1);
  const inner = Math.min(width, PAGE_WIDTH) - 2;
  for (let i = 0; i < pages; i += 1) {
    offsets[i + 1] = offsets[i] + inner / (aspects.get(i + 1) ?? PAGE_ASPECT) + 2 + PAGE_GAP;
    if (i > 0 && offsets[i + 1] - PAGE_GAP > most) return offsets.subarray(0, i + 1);
  }
  return offsets;
}

// The first and last pages with some of their height between from and to (in the list's own
// coordinates), each found by halving; last is first - 1 when none has.
export function pagesWithin(offsets, from, to) {
  const count = (before) => { // how many pages come before the first for which before(i) is true
    let low = 0;
    let high = offsets.length - 1;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (before(middle)) high = middle;
      else low = middle + 1;
    }
    return low;
  };
  return [count((i) => offsets[i + 1] - PAGE_GAP > from) + 1, count((i) => offsets[i] >= to)];
}

// What a PDF's page list mounts, in order: the pages from first to last (those within two screens
// of the view) and one more on each side, and each held page (heldPages: focus, the selection)
// with the one on each side, so Tab from a page always reaches the next one; between them, each
// stretch of pages not mounted as one spacer of their height, less the gap the list puts after it.
// Its length is bounded by the view's height, however many pages there are.
export function pageWindow(offsets, [first, last], held = []) {
  const pages = offsets.length - 1;
  const mounted = new Set();
  for (let page = first - 1; page <= last + 1; page += 1) mounted.add(page);
  for (const page of held) for (let near = page - 1; near <= page + 1; near += 1) mounted.add(near);
  const items = [];
  let next = 1; // the first page not yet laid out
  for (const page of [...mounted].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b)) {
    if (page > next) items.push({ spacer: next, height: offsets[page - 1] - offsets[next - 1] - PAGE_GAP });
    items.push({ page });
    next = page + 1;
  }
  if (next <= pages) items.push({ spacer: next, height: offsets[pages] - offsets[next - 1] - PAGE_GAP });
  return items;
}

// The part holding focus once one says focus came into it (inside) or left it: none once focus left
// the list, but the same while it goes on to another part of the list (toList), until that part says
// it came: a list that mounts only some parts (pageWindow) keeps the next one mounted meanwhile.
export function withFocus(current, part, inside, toList) {
  if (inside) return part;
  return current === part && !toList ? null : current;
}

// The parts near the view once one says whether it is: the same set when that changes nothing.
export function withNear(near, page, isNear) {
  if (near.has(page) === isNear) return near;
  const next = new Set(near);
  if (isNear) next.add(page);
  else next.delete(page);
  return next;
}

// What a page whose read failed says in its place (the catalog key): that its image was stopped at a
// ceiling, by name (backend/materials.py page_image), or else that it could not be shown.
const PAGE_CEILINGS = new Map([['memory_limit', 'paper.pageMemoryLimit'], ['step_limit', 'paper.pageStepLimit']]);
export function pageFailedKey(code) {
  return PAGE_CEILINGS.get(code) ?? 'paper.pageFailed';
}

// A PDF page as a data URL: the request carries the session, which an <img> could not, and the
// page's Content-Security-Policy admits data: images.
export async function pageImage(versionId, number, scale = 1.5, signal = undefined) {
  const blob = await getBlob(`/api/material-versions/${encodeURIComponent(versionId)}/pages/${number}?scale=${scale}`,
    signal); // aborted, the request goes away: the backend renders it no further than it has begun
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}
