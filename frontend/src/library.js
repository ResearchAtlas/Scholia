// The Library (slice-1 spec S7, F3a) as the interface reads and sends it: which files Scholia reads,
// how a paper's state and reason are named, its details as the edit form holds them, and where a
// passage's boxes sit on its page image (backend/materials.py, backend/extraction.py).
import { get, getBlob, post } from './api.js';

export const ACCEPT = '.pdf,.docx,.html,.htm,.xhtml,.md,.markdown,.tex,.latex';
const SUPPORTED = new Set(ACCEPT.split(','));
export const MAX_FILES = 20; // per request (backend/materials.py MAX_FILES)

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

// Adds files to a project: from a drop, Add files, or the conversation (conversationId), or as a
// new version of a material (materialId). Resolves to the backend's answer.
export async function addFiles(projectId, files, { conversationId, materialId } = {}) {
  const read = await Promise.all(files.map(readFile));
  return post(`/api/projects/${encodeURIComponent(projectId)}/materials`, {
    files: read, ...(conversationId ? { conversation_id: conversationId } : {}), ...(materialId ? { material_id: materialId } : {}),
  });
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

const REASONS = new Set(['ocr_waiting', 'no_text', 'not_read', 'stopped', 'time_limit', 'unreadable_file',
  'encrypted_file', 'file_missing', 'interrupted', 'not_found']);

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

// A passage's props for pointing at it: hovering it or focusing it from the keyboard highlights it
// (in the text and on its page alike), and Tab reaches it.
export function pointing(id, onPoint) {
  return { tabIndex: 0, onMouseEnter: () => onPoint(id), onMouseLeave: () => onPoint(null),
    onFocus: () => onPoint(id), onBlur: () => onPoint(null) };
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
