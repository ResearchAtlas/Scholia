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

export const indexStatus = (projectId, after) => get(`/api/projects/${encodeURIComponent(projectId)}/index`
  + (after ? `?after=${encodeURIComponent(after)}` : ''));

export const rebuildIndex = (projectId) => post(`/api/projects/${encodeURIComponent(projectId)}/index/rebuild`);

// A Rebuild asked while another index run ran (its answer's rebuild_pending) is the run that one's end records:
// once it has ended, the project's latest rebuild (status, the index status), when it was recorded after that
// run (followed by its id to its end, whatever ran since), else null (a Cancel of that run dropped it, R29).
export function rebuildAfter(started, ended, status) {
  if (!started.rebuild_pending || ended.status === 'cancelled') return null;
  const next = status?.last_rebuild;
  return next && next.after === started.run_id ? next.run_id : null;
}

// What a key pressed in the search field does: 'search' on Enter, except while an input method
// composes (Enter then confirms the Chinese characters chosen: 'compose'), 'clear' on Escape, else null.
export function fieldKey(event) {
  if (event.key === 'Escape') return 'clear';
  if (event.key === 'Enter') return event.isComposing || event.keyCode === 229 ? 'compose' : 'search';
  return null;
}

// The search field's keydown (a React event): Escape clears, its default prevented. An Enter that confirms
// composed characters keeps its default, so the input method commits them, and the form's submission it may
// bring is held off: returns whether to hold it off until the key comes up.
export function onFieldKey(event, clear) {
  const action = fieldKey({ key: event.key, isComposing: event.nativeEvent?.isComposing, keyCode: event.keyCode });
  if (action === 'clear') {
    event.preventDefault();
    clear();
  }
  return action === 'compose';
}

// The query the field sends, or null when it holds nothing visible (nothing is searched then).
export const queryOf = (text) => visible(text);

// What results say of how they were found, as [key, params], or null: keyword-only and why (the
// search's own reasons, else the helper's, as Settings words them), the index being rebuilt, or how
// much of the library meaning search covers so far.
const SEARCH_REASONS = new Set(['deadline', 'vectors_unavailable', 'index_unavailable', 'request_failed', 'input_refused', 'closing']);

// Why search is keyword-only, as a catalog key: the search's own reasons, else the helper's.
export const indexReason = (reason, offered = true) => (SEARCH_REASONS.has(reason) ? `search.reason.${reason}`
  : keywordOnlyReason({ search: { mode: 'keyword_only', reason } }, offered));

// Why an index run (a row of the background-run list) ended keyword-only, as a catalog key, worded as
// its own project offers the search model (the row's download_offered), not the project open now.
export const runReason = (run) => indexReason(run.result?.reason, run.download_offered !== false);

// What a finished index run did, as [key, params], or null: the embeddings it made and the project's
// as it ended, with the passages the search model could not take, or, keyword-only, the passages
// indexed for keyword search.
export function runCounts(run) {
  const passages = run.result?.passages;
  if (run.status !== 'succeeded' || !passages) return null;
  if (run.result.mode === 'keyword_only') return ['search.runKeyword', { indexed: passages.indexed }];
  const counts = { embedded: run.result.embedded ?? 0, done: passages.embedded, total: passages.embeddable };
  return passages.refused ? ['search.runHybridRefused', { ...counts, refused: passages.refused }] : ['search.runHybrid', counts];
}

// The index's passages under This project, as [key, params]: indexed and embedded, and how many the
// search model could not take, if any.
export const indexCounts = (passages) => [passages.refused ? 'search.indexCountsRefused' : 'search.indexCounts', passages];

// Whether every embeddable passage has been tried: embedded, or one the search model could not take.
const settled = (embedded, refused, total) => embedded + (refused ?? 0) >= total;

export function searchNote(found, offered = true) {
  if (!found) return null;
  if (found.mode === 'keyword_only') {
    return ['helper.keywordOnly', { reasonKey: indexReason(found.reason, offered) }]; // its text goes in as {reason}
  }
  if (found.index === 'building') return ['search.building', {}];
  const coverage = found.coverage;
  if (!coverage || coverage.embedded >= coverage.total) return null;
  return settled(coverage.embedded, coverage.refused, coverage.total) // all tried: the rest could not be taken
    ? ['search.partialRefused', { embedded: coverage.embedded, total: coverage.total, count: coverage.refused }]
    : ['search.partial', { embedded: coverage.embedded, total: coverage.total }];
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
// keywords (search is keyword-only now), embeddings so far (with any passages refused so far), indexed
// for both, or for both except the passages the search model could not take (or for keywords only,
// when it could take none of them).
export function paperIndexed(index, materialId) {
  const counts = index?.materials?.[materialId];
  if (!counts) return ['search.paperNotIndexed', {}];
  if (index.mode === 'keyword_only' || !counts.embeddable) return ['search.paperKeywordOnly', {}];
  if (!settled(counts.embedded, counts.refused, counts.embeddable)) { // with those it could not take, if any
    const done = { done: counts.embedded, total: counts.embeddable };
    return counts.refused ? ['search.paperEmbeddingRefused', { ...done, refused: counts.refused }] : ['search.paperEmbedding', done];
  }
  if (!counts.refused) return ['search.paperIndexed', {}];
  return counts.embedded ? ['search.paperRefused', { count: counts.refused }] : ['search.paperRefusedAll', { count: counts.refused }];
}

// Whether the index status is worth reading again soon: an index run is running, or the index is
// being rebuilt.
export const indexBusy = (index) => index?.run?.status === 'running' || index?.state === 'building';

// How long until the index status is read again while it is shown: soon while it is busy, else now
// and then, since the search model can arrive from elsewhere (a download, an import) at any time.
export const indexPoll = (index) => (indexBusy(index) ? 1500 : 5000);

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
