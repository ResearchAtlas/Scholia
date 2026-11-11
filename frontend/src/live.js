// Turns streaming now, by conversation. They live outside the conversation view so that
// opening another conversation does not end them: the backend stops a turn whose stream
// closes (backend/runs.py stream_closed), and several conversations may run at once
// (slice-1 spec section 2).
import { useSyncExternalStore } from 'react';
import { get, post, stream } from './api.js';
import { nextQueued } from './followup.js';
import { asksChanged } from './library.js';
import { memoryChanged } from './memory.js';

const turns = new Map(); // conversation id -> { text, runId, answer, resultSaved, error, limit, done }
const aborts = new Map();
const latest = new WeakMap(); // S1-33: each stream's own latest state (by its abort controller), owned or not
const replaced = new WeakMap(); // and the stream it replaced
const notAdmitted = new WeakSet(); // the streams known never to have started a turn: refused, or stopped before it
const ended = new WeakSet(); // the streams whose own end (their finally) has run
const owed = new WeakMap(); // a stream that succeeded while a newer one was sent over it: the drain it left to that one

// What a stream not admitted gives the conversation back to: the latest stream it replaced still under way (one whose
// first event is unread may be an admitted turn), else the latest that finished; never one known not admitted.
function givenBack(abort) {
  let finished = null;
  for (let owner = replaced.get(abort); owner; owner = replaced.get(owner)) {
    if (notAdmitted.has(owner)) continue;
    if (!latest.get(owner).done) return owner;
    finished ??= owner;
  }
  return finished;
}
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
// A stream owns its conversation's live state while it is the latest sent there: an older one never overwrites or clears
// a newer one's, and one refused gives back what it replaced (S1-33, PR 52 review 1), so Stop always reaches the turn shown.
export function send(conversationId, path, body, text) {
  const abort = new AbortController();
  let turn = { text, runId: null, answer: null, done: false };
  const mine = () => aborts.get(conversationId) === abort;
  replaced.set(abort, aborts.get(conversationId));
  aborts.set(conversationId, abort);
  latest.set(abort, turn);
  set(conversationId, turn, 'send');
  return new Promise((admitted, refused) => {
    stream(path, body, (event) => {
      turn = apply(turn, event);
      if (event.type === 'ask') asksChanged(conversationId); // S1-19c: the agent asks; the conversation shows it
      if (event.type === 'memory') memoryChanged(null); // S1-21a: a command or the agent's note changed memory
      // S1-19c: the help skill opens a place, only while its turn's conversation and project are shown (CT4)
      if (event.type === 'open_place' && shown?.conversationId === conversationId && shown.projectId === event.project_id) {
        openPlace(event.place);
      }
      if (event.type === 'chat_response' && event.result_saved === false && turn.runId) {
        unsaved.set(turn.runId, { text: event.content, cut: Boolean(event.cut), ...(event.notices ? { notices: event.notices } : {}) });
      }
      latest.set(abort, turn);
      if (mine()) set(conversationId, turn, event.type);
      if (turn.runId) admitted();
    }, abort.signal).catch((error) => {
      if (turn.runId) return;
      const stopped = error.name === 'AbortError';
      notAdmitted.add(abort);
      if (mine()) {
        const owner = givenBack(abort);
        const prior = owner && latest.get(owner);
        if (prior && !(stopped && prior.done)) { // the turn it replaced comes back as it is now, finished or under way
          // One whose own end is still to come owns the conversation again, so that end finishes it here (and drains);
          // one that ended while this was sent over it is done, and the drain it left starts now (PR 52 cycle 2)
          const over = prior.done && ended.has(owner);
          if (over) aborts.delete(conversationId); else aborts.set(conversationId, owner);
          set(conversationId, prior, 'restored');
          if (over) {
            const start = owed.get(owner);
            owed.delete(owner);
            start?.();
          }
        } else if (!stopped) {
          set(conversationId, null, 'refused');
        }
      }
      if (!stopped) refused(error);
    }).finally(() => {
      if (!turn.done) turn = { ...turn, done: true };
      latest.set(abort, turn);
      ended.add(abort);
      if (!mine()) { // a newer stream was sent over it: one refused gives it back and starts the drain it left
        if (turn.status === 'succeeded') owed.set(abort, () => drain(conversationId, turn.runId, body));
        return admitted();
      }
      aborts.delete(conversationId);
      if (turns.get(conversationId) && turns.get(conversationId) !== turn) set(conversationId, turn, 'run_finished');
      admitted();
      if (turn.status === 'succeeded') drain(conversationId, turn.runId, body);
    });
  });
}

// S1-33 (F11, queue): after a turn that succeeded, the oldest message waiting in its conversation starts as the next
// turn, on the route the turn was sent on, wherever the window is; the backend starts it only while that turn is still
// the latest and succeeded, and once. After any other ending it waits with Send.
export async function drain(conversationId, runId, route = {}) {
  const current = turns.get(conversationId);
  if (!runId || (current && !current.done)) return; // a turn is already starting or running here

  try {
    const next = nextQueued(await get(`/api/conversations/${encodeURIComponent(conversationId)}`), runId);
    const now = turns.get(conversationId);
    if (now && !now.done) return; // a turn started here while the conversation was read: it is never disturbed
    const { model, provider, effort } = route;
    if (next) {
      await send(conversationId, `/api/conversations/${encodeURIComponent(conversationId)}/queue/${encodeURIComponent(next.id)}/send`,
        { model, provider, effort, after: runId }, next.text);
    }
  } catch {
    // refused (started meanwhile, or no longer after a turn that succeeded): it waits, as the conversation shows
  }
}

// Stop: cancels the run, which then finishes its stream; before the run has started,
// closing the stream stops it. runId, if given, is a turn the conversation's record shows running. A send still
// waiting to be admitted may have been sent over a turn under way: Stop reaches every stream of the conversation under
// way here, not only the latest, so the turn that runs is always stopped (S1-33, PR 52 cycle 2).
export async function stop(conversationId, runId) {
  const runs = new Set(runId ? [runId] : []);
  const closing = [];
  for (let owner = aborts.get(conversationId); owner; owner = replaced.get(owner)) {
    if (notAdmitted.has(owner) || latest.get(owner).done) continue;
    if (latest.get(owner).runId) runs.add(latest.get(owner).runId); else closing.push(owner);
  }
  for (const owner of closing) owner.abort();
  await Promise.all([...runs].map((id) => post(`/api/runs/${id}/cancel`)));
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
