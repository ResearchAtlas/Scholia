// Turns streaming now, by conversation. They live outside the conversation view so that
// opening another conversation does not end them: the backend stops a turn whose stream
// closes (backend/runs.py stream_closed), and several conversations may run at once
// (slice-1 spec section 2).
import { useSyncExternalStore } from 'react';
import { post, stream } from './api.js';

const turns = new Map(); // conversation id -> { text, runId, answer, resultSaved, error, limit, done }
const aborts = new Map();
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
    case 'chat_response': return { ...turn, answer: event.content, resultSaved: event.result_saved };
    case 'error': return { ...turn, error: event.code };
    case 'limit_reached': return { ...turn, limit: true };
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

export function clear(conversationId) {
  if (turns.get(conversationId)?.done) set(conversationId, null, 'cleared');
}

export function useLiveTurn(conversationId) {
  return useSyncExternalStore(subscribe, () => (conversationId ? turns.get(conversationId) ?? null : null));
}
