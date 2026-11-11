// S1-21a: memory as the window shows it (backend/memory.py; slice-1 spec F10). What a turn's memory command or the
// agent's note did, read back from the record (the conversation's turns carry it); project state's progress in words;
// Where we left off's dismissal for this return; and the message box filled by its next-step button (nothing is sent
// until Send).
import { ApiError, del, get, patch, post } from './api.js';
import { libraryChanged } from './library.js';
import { errorText } from './text.js';

// A turn's memory actions (backend/runs.py Harness._memory_command, update_progress), as catalog keys.
export const MEMORY_ACTIONS = { saved: 'memory.saved', updated: 'memory.updated', forgotten: 'memory.forgotten',
  memory_off: 'memory.memoryOff', no_match: 'memory.noMatch', no_candidate: 'memory.noCandidate',
  declined: 'memory.declined', noted: 'memory.noted' };

// One action's lines: a saved or updated record's own text, read back from it; else the action in words.
export function actionLines(t, item) {
  const key = MEMORY_ACTIONS[item.action];
  if (!key) return [];
  if ((item.action === 'saved' || item.action === 'updated') && item.records?.length) {
    return item.records.map((record) => t(key, { content: record.content }));
  }
  if (item.action === 'saved' || item.action === 'updated') return [t(`${key}Live`)];
  return [t(key, { count: item.count ?? 0 })];
}

// A record's text: progress in the interface's words (it is written by code, from AAB's records), else its own.
export function recordText(t, record) {
  const event = record.structured?.event;
  if (record.type === 'progress' && event) {
    return t(`memory.progress.${event}`, { count: record.structured.count ?? 0, date: record.structured.date ?? '' });
  }
  return record.content;
}

// Where we left off shows once per return to a project: dismissed until a newer turn.
const dismissed = new Set();
export const leftOffDismissed = (projectId, latest) => dismissed.has(`${projectId}:${latest}`);
export const dismissLeftOff = (projectId, latest) => dismissed.add(`${projectId}:${latest}`);

// Memory changed (a record saved, kept, edited or forgotten, here or by a turn): the library's one change, so every
// view, question and read that drops and reads again when a paper is deleted does so too (Harold, 2026-10-21: no
// client-held derived state; S1-21a fix batch 4: one invalidation path). projectId: the project, or null for any.
export const memoryChanged = (projectId = null) => libraryChanged(projectId);

// The message box's text, put there by the next-step button: the conversation's composer takes it.
export const composerFill = new EventTarget();
export function fillComposer(text) {
  composerFill.dispatchEvent(new CustomEvent('fill', { detail: text }));
}

export const readLeftOff = (projectId) => get(`/api/projects/${encodeURIComponent(projectId)}/left-off`);
export const readMemory = (projectId) => get(`/api/memory${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`);
export const changeRecord = (record, body) => patch(`/api/memory/${encodeURIComponent(record.id)}`, { version: record.version, ...body });
export const forgetRecord = (record) => del(`/api/memory/${encodeURIComponent(record.id)}?version=${record.version}`);
export const review = (body) => post('/api/memory/review', body);

// A memory action from the views: null once done, else what was not saved: the error in words and its code, whether it
// was an edit (edit), and the record it was for (id). Never the text typed: that stays in its open editor (MB1, Q16).
export async function attempt(t, action, edit = false, id = null) {
  try {
    await action();
    return null;
  } catch (error) {
    const code = error instanceof ApiError ? error.code : 'internal';
    return { problem: errorText(t, code), code, edit: Boolean(edit), id };
  }
}

// Whether the views read again after an action: once done, or when it failed because the record changed (then the
// editor closes, with the notice). A failure that changed nothing leaves the view, and its open editor, as it is.
const CHANGED = new Set(['version_changed', 'out_of_date', 'not_found']);
export const readsAgain = (failed) => !failed || CHANGED.has(failed.code);

// What an action could not save, kept by view and project (`settings:<project>`, `leftOff:<project>`) outside the
// components that show it, so a remount never drops it (Q12, PR 51 recheck 3); only Dismiss, or a later failure in the
// same view, replaces it. keptEvents tells the views when one changes.
const kept = new Map();
export const keptEvents = new EventTarget();
export const keptUnsaved = (key) => kept.get(key) ?? null;
export function keepUnsaved(key, value) {
  if (value) kept.set(key, value);
  else kept.delete(key);
  keptEvents.dispatchEvent(new Event('changed'));
}

// The kept notice after an action on record id: what it could not save (failed), or, once that record's action
// succeeded, nothing; another record's success leaves it.
export const settled = (kept, failed, id) => failed ?? (id != null && kept?.id === id ? null : kept);
