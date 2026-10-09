// Search over a project's papers (backend/search.py; slice-1 spec S7 and section 7.2): the request,
// what a key does in the search field, what results say of how they were found, the index's lines
// under a paper's Details, and opening a paper at a result's passage.
import { get, post } from './api.js';
import { keywordOnlyReason } from './helper.js';
import { PASSAGE_STRETCH } from './library.js';
import { errorText, visible } from './text.js';

export const LIBRARY_LIMIT = 20; // results the Library shows (the API's default is the answer's 8)

export const searchProject = (projectId, query) =>
  post(`/api/projects/${encodeURIComponent(projectId)}/search`, { query, limit: LIBRARY_LIMIT });

export const indexStatus = (projectId) => get(`/api/projects/${encodeURIComponent(projectId)}/index`);

export const rebuildIndex = (projectId) => post(`/api/projects/${encodeURIComponent(projectId)}/index/rebuild`);

// What a key pressed in the search field does: 'search' on Enter, except while an input method
// composes (Enter then confirms the Chinese characters chosen), 'clear' on Escape, else null.
export function fieldKey(event) {
  if (event.key === 'Escape') return 'clear';
  if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) return 'search';
  return null;
}

// The query the field sends, or null when it holds nothing visible (nothing is searched then).
export const queryOf = (text) => visible(text);

// What results say of how they were found, as [key, params], or null: keyword-only and why (the
// search's own reasons, else the helper's, as Settings words them), the index being rebuilt, or how
// much of the library meaning search covers so far.
const SEARCH_REASONS = new Set(['deadline', 'vectors_unavailable', 'index_unavailable', 'request_failed', 'closing']);

export function searchNote(found, offered = true) {
  if (!found) return null;
  if (found.mode === 'keyword_only') {
    const reason = SEARCH_REASONS.has(found.reason) ? `search.reason.${found.reason}`
      : keywordOnlyReason({ search: found }, offered);
    return ['helper.keywordOnly', { reasonKey: reason }]; // the reason's own text goes in as {reason}
  }
  if (found.index === 'building') return ['search.building', {}];
  const coverage = found.coverage;
  if (coverage && coverage.embedded < coverage.total) return ['search.partial', { embedded: coverage.embedded, total: coverage.total }];
  return null;
}

// Where a result's passage is, for its line: its page, then its section path.
export function whereIs(t, result) {
  const parts = [];
  if (result.page != null) parts.push(t('paper.onPage', { number: result.page }));
  const path = (result.section_path ?? []).filter(Boolean).join(' › ');
  if (path) parts.push(path);
  return parts.join(' · ');
}

// A paper's line under Details for the search index, as [key, params]: not indexed yet, indexed for
// keywords (search is keyword-only now), embeddings so far, or indexed for both.
export function paperIndexed(index, materialId) {
  const counts = index?.materials?.[materialId];
  if (!counts) return ['search.paperNotIndexed', {}];
  if (index.mode === 'keyword_only' || !counts.embeddable) return ['search.paperKeywordOnly', {}];
  if (counts.embedded < counts.embeddable) return ['search.paperEmbedding', { done: counts.embedded, total: counts.embeddable }];
  return ['search.paperIndexed', {}];
}

// Whether the index status is worth reading again soon: an index run is running.
export const indexBusy = (index) => index?.run?.status === 'running' || index?.state === 'building';

// Bring a passage of the paper's text view into view and focus it (which highlights it), once its
// stretch of passages (PASSAGE_STRETCH, read only near the view) has loaded: the stretch is scrolled
// to first, then the passage, when it shows. Returns a function that stops waiting.
export function revealPassage(root, target, { wait = (fn) => setTimeout(fn, 50), tries = 100 } = {}) {
  let stopped = false;
  const stretch = root?.querySelector(`[data-part="${Math.floor(target.ordinal / PASSAGE_STRETCH)}"]`);
  stretch?.scrollIntoView({ block: 'start' });
  const look = (left) => {
    if (stopped || !root) return;
    const passage = root.querySelector(`[data-passage="${CSS.escape(target.id)}"]`);
    if (passage) {
      passage.scrollIntoView({ block: 'center' });
      passage.focus({ preventScroll: true });
    } else if (left > 0) {
      wait(() => look(left - 1));
    }
  };
  look(tries);
  return () => { stopped = true; };
}

// What a finished offer of the search model says it did, in the interface's words, or null: the
// answer's outcome (a download started, or why none was), or Later.
export function offerOutcome(t, result) {
  const said = result?.outcome ?? result?.answer;
  if (!said) return null;
  const key = `search.offer.${said}`;
  const text = t(key);
  return text === key ? errorText(t, said) : text;
}
