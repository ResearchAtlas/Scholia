// The local model helper and its models as the interface shows them (backend/local_helper.py,
// GET /api/helper): the search model's download and import, the helper's state, and whether
// search is keyword-only.

export const SOURCES = ['huggingface', 'modelscope'];

// The mirror the consent screen selects first: ModelScope once Hugging Face could not be
// reached, else the one chosen last time, else Hugging Face.
export function preferredSource(status) {
  return status?.recommended_source ?? status?.model_source ?? 'huggingface';
}

// How far a download has come, from 0 to 1.
export function progress(download) {
  if (!download?.total) return 0;
  return Math.min(1, Math.max(0, download.received / download.total));
}

export const downloading = (status) => status?.download?.state === 'running';

// Whether Settings, Advanced offers the search model's download while `project` is the current
// project. A Local only project offers none, only the import (ticket 71); any other project, the
// General project among them, or none offers it. Downloads are app-wide either way.
export const downloadOffered = (project) => project?.sensitivity !== 'local_only';

// Why search is keyword-only, as a catalog key, or null when it can use the search model. Where no
// download is offered, a missing or changed model's advice names the import only.
const REASONS = new Set(['model_missing', 'model_changed', 'binary_missing', 'binary_changed', 'start_timeout',
  'start_failed', 'crashed', 'unhealthy', 'helper_failed']);
const IMPORT_ONLY = new Set(['model_missing', 'model_changed']);

export function keywordOnlyReason(status, offered = true) {
  const search = status?.search;
  if (!search || search.mode !== 'keyword_only') return null;
  const reason = REASONS.has(search.reason) ? search.reason : 'other';
  return `helper.${offered || !IMPORT_ONLY.has(reason) ? 'reason' : 'reasonImport'}.${reason}`;
}

// The helper's state as the section names it: stopped while search is keyword-only is "unavailable",
// since no start is coming until what the search row names is fixed.
export function helperState(status) {
  const state = status?.helper?.state ?? 'stopped';
  return state === 'stopped' && keywordOnlyReason(status) ? 'unavailable' : state;
}

// How often the section reads the status: often while a download or a start is under way.
export function pollDelay(status) {
  return downloading(status) || ['starting', 'restarting'].includes(status?.helper?.state) ? 1000 : 4000;
}

// The window's file picker for a model file, or null where there is none (a browser).
export function modelFilePicker(win = globalThis.window) {
  const api = win?.pywebview?.api;
  return typeof api?.choose_model_file === 'function' ? () => api.choose_model_file() : null;
}
