// The Library (slice-1 spec S7, F3a) as the interface reads and sends it: which files Scholia reads,
// how a paper's state and reason are named, its details as the edit form holds them, and where a
// passage's boxes sit on its page image (backend/materials.py, backend/extraction.py).
import { ApiError, get, getBlob, post } from './api.js';

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
// new version of a material (materialId), however many. A file larger than Scholia reads refuses
// the selection, as the backend would; the rest go in as few requests as fit (requestsOf), read
// one request at a time, and stay one batch, which the backend holds in the drop's lookup run: each
// request but the last says more follow (more), the ones after the first name the batch the first
// answered (batch), and the last closes it, so a Local only project asks once, counting every
// identifier the drop gave. A batch the window never closes (it went away, or a request failed)
// closes itself on the backend. Resolves to the backend's answers together; a request that fails
// once others were added ends the sending, its error code in `problem`.
export async function addFiles(projectId, files, { conversationId, materialId } = {}) {
  if (files.some((file) => file.size > MAX_FILE_BYTES)) throw new ApiError(413, 'file_too_large');
  const send = (body) => post(`/api/projects/${encodeURIComponent(projectId)}/materials`, {
    ...body, ...(conversationId ? { conversation_id: conversationId } : {}), ...(materialId ? { material_id: materialId } : {}),
  });
  const groups = requestsOf(files);
  const added = { materials: [], lookup_run_id: null };
  for (const [i, group] of groups.entries()) {
    const batch = added.lookup_run_id ? { batch: added.lookup_run_id } : {};
    try {
      const answer = await send({ files: await Promise.all(group.map(readFile)), ...batch,
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

// A paper's state (reading, ready, needs_attention) and its reason, as catalog keys.
export function stateKey(material) {
  return `library.state.${material.state}`;
}

// A finished lookup's outcome for a paper, each with its text (library.source.*): what its details say.
export const LOOKUP_OUTCOMES = ['no_identifier', 'not_read', 'not_found', 'unavailable', 'refused'];

const REASONS = new Set(['ocr_waiting', 'no_text', 'not_read', 'stopped', 'time_limit', 'unreadable_file',
  'encrypted_file', 'file_missing', 'interrupted', 'not_found', 'outdated']);

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

// Every passage of a version, read a page of the API at a time.
export async function loadPassages(versionId) {
  const all = [];
  for (let offset = 0; ; offset += 500) {
    const page = await get(`/api/material-versions/${encodeURIComponent(versionId)}/passages?offset=${offset}&limit=500`);
    all.push(...page.passages);
    if (!page.passages.length || all.length >= page.total) return all;
  }
}

// Passages grouped by page, for the page viewer.
export function byPage(passages) {
  const pages = new Map();
  for (const passage of passages) {
    if (passage.page == null) continue;
    if (!pages.has(passage.page)) pages.set(passage.page, []);
    pages.get(passage.page).push(passage);
  }
  return pages;
}

// A PDF page as a data URL: the request carries the session, which an <img> could not, and the
// page's Content-Security-Policy admits data: images.
export async function pageImage(versionId, number, scale = 1.5) {
  const blob = await getBlob(`/api/material-versions/${encodeURIComponent(versionId)}/pages/${number}?scale=${scale}`);
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}
