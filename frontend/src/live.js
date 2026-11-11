// Turns streaming now, by conversation. They live outside the conversation view so that
// opening another conversation does not end them: the backend stops a turn whose stream
// closes (backend/runs.py stream_closed), and several conversations may run at once
// (slice-1 spec section 2).
import { useSyncExternalStore } from 'react';
import { ApiError, get, post, stream } from './api.js';
import { nextQueued } from './followup.js';
import { asksChanged } from './library.js';
import { memoryChanged } from './memory.js';

const turns = new Map(); // conversation id -> { text, runId, answer, resultSaved, error, limit, done }
// S1-33 (PR 52 cycle 3, design review B): one stream at a time per conversation here, its abort controller; while one is
// being admitted or runs, a message is refused here (send), so nothing is ever handed back between streams.
const aborts = new Map();
const routes = new Map(); // run id -> the route its turn was sent on here ({ model, provider, effort })
const halted = new Set(); // conversations Stop was pressed in here since a turn the researcher started was admitted
const stops = new Map(); // conversation id -> how many times Stop was pressed in it here
const followed = new Set(); // run ids a waiting message was started after, here (one start a turn)
const idlers = new Map(); // conversation id -> what waits for its stream here to end (idle)
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
    case 'memory': return { ...turn, memory: [...(turn.memory ?? []), { action: event.action, count: event.count }] }; // S1-21a
    // S1-33: a step carried steering notes, a message joined the queue, or a settlement reached a budget notice: the
    // view reads the record again
    case 'steering_note': return { ...turn, steered: (turn.steered ?? 0) + 1 };
    case 'queued': return { ...turn, queued: (turn.queued ?? 0) + 1 };
    case 'budget': return { ...turn, budget: (turn.budget ?? 0) + 1 };
    case 'run_finished': return { ...turn, done: true, status: event.status };
    default: return turn;
  }
}

// Sends a message (or continues a turn) and follows its stream. Resolves once the turn is
// admitted, or rejects with the refusal when it was not; an admitted turn ends done
// however its stream ends, and the conversation's saved turns tell what happened.
// S1-33 (PR 52 cycle 3): while a turn is being admitted or runs here in the conversation, it is refused here at once,
// with no request, as the backend refuses a second turn (409 active_run): the message then follows the setting, steered
// or queued (ConversationView), and the one stream here keeps the conversation, so Stop always reaches the turn shown.
export function send(conversationId, path, body, text) {
  if (aborts.has(conversationId)) return Promise.reject(new ApiError(409, 'active_run'));
  const abort = new AbortController();
  let turn = { text, runId: null, answer: null, done: false };
  aborts.set(conversationId, abort);
  const release = () => { // the conversation is free here again, said before anything awaiting this stream goes on
    if (aborts.get(conversationId) !== abort) return false;
    aborts.delete(conversationId);
    for (const resolve of idlers.get(conversationId) ?? []) resolve();
    idlers.delete(conversationId);
    return true;
  };
  set(conversationId, turn, 'send');
  return new Promise((admitted, refused) => {
    stream(path, body, (event) => {
      turn = apply(turn, event);
      if (event.type === 'run_started') {
        routes.set(event.run_id, { model: body?.model, provider: body?.provider, effort: body?.effort });
        if (!body?.after) halted.delete(conversationId); // the researcher's own turn, admitted: waiting ones may follow it
      }
      if (event.type === 'ask') asksChanged(conversationId); // S1-19c: the agent asks; the conversation shows it
      if (event.type === 'memory') memoryChanged(null); // S1-21a: a command or the agent's note changed memory
      // S1-19c: the help skill opens a place, only while its turn's conversation and project are shown (CT4)
      if (event.type === 'open_place' && shown?.conversationId === conversationId && shown.projectId === event.project_id) {
        openPlace(event.place);
      }
      if (event.type === 'chat_response' && event.result_saved === false && turn.runId) {
        unsaved.set(turn.runId, { text: event.content, cut: Boolean(event.cut), ...(event.notices ? { notices: event.notices } : {}) });
      }
      set(conversationId, turn, event.type);
      if (turn.runId) admitted();
    }, abort.signal).catch((error) => {
      if (turn.runId) return;
      if (error.name === 'AbortError') return; // stopped before it was admitted: it ends done, starting nothing
      release();
      set(conversationId, null, 'refused');
      refused(error);
    }).finally(() => {
      if (!turn.done) turn = { ...turn, done: true };
      if (release() && turns.get(conversationId) && turns.get(conversationId) !== turn) set(conversationId, turn, 'run_finished');
      admitted();
      if (turn.status === 'succeeded') drain(conversationId);
    });
  });
}

// S1-33 (F11, queue; PR 52 cycle 3, design review B): after a turn that succeeded, the oldest message waiting in its
// conversation starts as the next turn, on the route that turn was sent on, wherever the window is. Each call reads the
// conversation and acts on what the record says now: only while the latest turn there succeeded and was sent from this
// window, no Stop was pressed here since, and nothing is being admitted or running here, checked before and after the
// read; once a turn. A message edited or removed while it was being started (not_queued) gives way to the next, read
// again. The backend starts it only while that turn is still the latest, succeeded and not stopped, and once. After any
// other ending it waits with Send.
export async function drain(conversationId) {
  const pressed = stopsAt(conversationId);
  const free = () => !aborts.has(conversationId) && !halted.has(conversationId) && stopsAt(conversationId) === pressed;
  for (let tries = 0; tries < 3 && free(); tries += 1) {
    let read;
    try {
      read = await get(`/api/conversations/${encodeURIComponent(conversationId)}`);
    } catch {
      return; // it waits, as the conversation shows when it is read
    }
    const after = read?.turns?.at(-1)?.run_id;
    const next = routes.has(after) ? nextQueued(read, after) : null; // a turn sent elsewhere: that window starts it
    if (!next || !free() || (tries === 0 && followed.has(after))) return;
    followed.add(after);
    try {
      await send(conversationId, `/api/conversations/${encodeURIComponent(conversationId)}/queue/${encodeURIComponent(next.id)}/send`,
        { ...routes.get(after), after }, next.text);
      return;
    } catch (error) {
      if (!(error instanceof ApiError && error.code === 'not_queued')) return; // refused otherwise: it waits with Send
    }
  }
}

// Stop: cancels the run, which then finishes its stream; before the run has started,
// closing the stream stops it. runId, if given, is a turn the conversation's record shows running (from another window,
// perhaps). S1-33 (PR 52 cycle 3): from now on nothing waiting starts on its own here until a turn the researcher starts
// here is admitted, and the backend starts nothing after a turn stopped anywhere (Harness.cancel).
export async function stop(conversationId, runId) {
  halted.add(conversationId);
  stops.set(conversationId, stopsAt(conversationId) + 1);
  const runs = new Set(runId ? [runId] : []);
  const live = turns.get(conversationId);
  if (aborts.has(conversationId) && live && !live.done) {
    if (live.runId) runs.add(live.runId); else aborts.get(conversationId).abort();
  }
  await Promise.all([...runs].map((id) => post(`/api/runs/${id}/cancel`)));
}

// S1-33: how many times Stop was pressed in the conversation here, so the view can tell whether one came meanwhile.
export function stopsAt(conversationId) {
  return stops.get(conversationId) ?? 0;
}

// S1-33: whether Stop was pressed in the conversation here since a turn the researcher started there was admitted.
export function halt(conversationId) {
  return halted.has(conversationId);
}

// S1-33: resolves once no turn is being admitted or running here in the conversation.
export function idle(conversationId) {
  if (!aborts.has(conversationId)) return Promise.resolve();
  return new Promise((resolve) => idlers.set(conversationId, [...(idlers.get(conversationId) ?? []), resolve]));
}

// S1-19c: open_place's places (backend/agent.py PLACES): the window opens the one named (Shell.jsx).
export const PLACES = ['library', 'settings.general', 'settings.providers', 'settings.subagents', 'settings.project',
  'settings.advanced'];
export const places = new EventTarget();
let shown = null; // the conversation the window shows now and its project (ConversationView), or null
// Says which conversation the window shows; returns what says it is no longer shown.
export function showing(projectId, conversationId) {
  const now = conversationId ? { projectId, conversationId } : null;
  shown = now;
  return () => { if (shown === now) shown = null; };
}
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
