// Turns streaming now, by conversation. They live outside the conversation view so that
// opening another conversation does not end them: the backend stops a turn whose stream
// closes (backend/runs.py stream_closed), and several conversations may run at once
// (slice-1 spec section 2).
import { useSyncExternalStore } from 'react';
import { post, stream } from './api.js';
import { asksChanged } from './library.js';

const turns = new Map(); // conversation id -> { text, runId, answer, resultSaved, error, limit, done }
const aborts = new Map();
const unsaved = new Map(); // run id -> an answer shown but not saved (backend/runs.py), { text, cut, notices }, for this window
const listeners = new Set();

function set(conversationId, turn, eventType) {
  if (turn) turns.set(conversationId, turn);
  else turns.delete(conversationId);
  for (const listener of listeners) listener(conversationId, turn, eventType);
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// A live turn after one stream event.
export function apply(turn, event) {
  switch (event.type) {
    case 'run_started': return { ...turn, runId: event.run_id };
    case 'chat_response': return { ...turn, answer: event.content, resultSaved: event.result_saved, ...(event.cut ? { cut: true } : {}),
      ...(event.notices ? { notices: event.notices } : {}) }; // S1-19b: keyword-only, unchecked, no skills, retracted
    case 'error': return { ...turn, error: event.code };
    case 'limit_reached': return { ...turn, limit: event.limit ?? 'budget' }; // S1-19c: a turn limit, or the budget
    case 'tool_call': return { ...turn, tool: event.name }; // S1-19c: the tool the agent is using now
    case 'step': { const { tool, ...rest } = turn; return rest; } // the next model call: no tool is in use (CT3)
    case 'skill': return { ...turn, skills: [...(turn.skills ?? []), event.name] };
    case 'run_finished': return { ...turn, done: true };
    default: return turn;
  }
}

// Sends a message (or continues a turn) and follows its stream. Resolves once the turn is
// admitted, or rejects with the refusal when it was not; an admitted turn ends done
// however its stream ends, and the conversation's saved turns tell what happened.
export function send(conversationId, path, body, text) {
  const abort = new AbortController();
  let turn = { text, runId: null, answer: null, done: false };
  aborts.set(conversationId, abort);
  set(conversationId, turn, 'send');
  return new Promise((admitted, refused) => {
    stream(path, body, (event) => {
      turn = apply(turn, event);
      if (event.type === 'ask') asksChanged(conversationId); // S1-19c: the agent asks; the conversation shows it
      if (event.type === 'open_place') openPlace(event.place); // S1-19c: the help skill opens a place
      if (event.type === 'chat_response' && event.result_saved === false && turn.runId) {
        unsaved.set(turn.runId, { text: event.content, cut: Boolean(event.cut), ...(event.notices ? { notices: event.notices } : {}) });
      }
      set(conversationId, turn, event.type);
      if (turn.runId) admitted();
    }, abort.signal).catch((error) => {
      if (turn.runId || error.name === 'AbortError') return;
      set(conversationId, null, 'refused');
      refused(error);
    }).finally(() => {
      aborts.delete(conversationId);
      if (turns.get(conversationId) && !turn.done) set(conversationId, { ...turn, done: true }, 'run_finished');
      admitted();
    });
  });
}

// Stop: cancels the run, which then finishes its stream; before the run has started,
// closing the stream stops it.
export async function stop(conversationId, runId) {
  const id = runId ?? turns.get(conversationId)?.runId;
  if (id) await post(`/api/runs/${id}/cancel`);
  else aborts.get(conversationId)?.abort();
}

// S1-19c: open_place's places (backend/agent.py PLACES): the window opens the one named (Shell.jsx).
export const PLACES = ['library', 'settings.general', 'settings.providers', 'settings.subagents', 'settings.project',
  'settings.advanced'];
export const places = new EventTarget();
export function openPlace(place) {
  if (PLACES.includes(place)) places.dispatchEvent(new CustomEvent('open', { detail: place }));
}

// A turn stopped at a limit (backend/runs.py): its catalog key; the budget's when it names none.
export const LIMITS = { agent_steps: 'turn.limitSteps', tool_calls: 'turn.limitToolCalls', turn_minutes: 'turn.limitMinutes',
  chain_over_window: 'turn.limitChain' };
export function limitText(code) {
  return LIMITS[code] ?? 'turn.limit';
}

export function unsavedAnswer(runId) {
  return unsaved.get(runId) ?? null;
}

export function clear(conversationId) {
  if (turns.get(conversationId)?.done) set(conversationId, null, 'cleared');
}

export function useLiveTurn(conversationId) {
  return useSyncExternalStore(subscribe, () => (conversationId ? turns.get(conversationId) ?? null : null));
}

// S1-19b: an answer's notices (backend/runs.py), as catalog keys: what the answer could not do, in words.
export const NOTICES = { index_unavailable: 'turn.notice.indexUnavailable',
  unchecked_budget: 'turn.notice.uncheckedBudget', keyword_only: 'turn.notice.keywordOnly', unchecked: 'turn.notice.unchecked',
  skills_unavailable: 'turn.notice.noSkills', retracted: 'turn.notice.retracted' };
