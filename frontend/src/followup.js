// S1-33 (slice-1 spec F11): messages sent while a turn runs, and the budget bar. A message sent then follows the one
// setting `[ui] follow_up`, read as it is sent: steer posts it as a steering note the agent reads at its next step,
// queue waits as the next turn. Neither path ever answers 409 active_run; what each did is read back from the
// conversation's record before the window says so (backend/app.py).
import { ApiError, get, post } from './api.js';

// A failed read rejects: the message is then not sent, never sent the other way (PR 52 cycle 2).
export async function followUpMode() {
  return (await get('/api/settings')).values?.ui?.follow_up === 'queue' ? 'queue' : 'steer';
}

// Sends text while a turn runs (runId, or null while it is still being admitted): { steering } or { queued }. One that
// names papers (materialIds) waits as the next turn with them, whatever the setting: a note cannot narrow a turn already
// under way (Harold, 2026-11-11; SPEC:281).
export async function follow(conversationId, runId, text, materialIds = []) {
  if (runId && !materialIds.length && (await followUpMode()) === 'steer') {
    return post(`/api/runs/${encodeURIComponent(runId)}/steering`, { content: text });
  }
  return { queued: await post(`/api/conversations/${encodeURIComponent(conversationId)}/queue`,
    { content: text, ...(materialIds.length ? { material_ids: materialIds } : {}) }) };
}

// The queued message a finished turn starts, when that turn succeeded: the oldest, or none.
export function nextQueued(read, runId) {
  const latest = read?.turns?.at(-1);
  if (!latest || latest.run_id !== runId || latest.status !== 'succeeded') return null;
  return read.queued?.[0] ?? null;
}

// The bar (F11, The bar): the budget whose settled spend is the larger share of its limit, from the lowest notice
// percent on; null below it, and with no notice percents. { budget, settled, limit, percent, meter (at most 100), marks }.
export function budgetBar(spend) {
  if (!spend) return null;
  const shares = ['conversation', 'project'].map((budget) => spend[budget] && spend[budget].limit_usd > 0
    ? { budget, settled: spend[budget].settled_usd, limit: spend[budget].limit_usd,
      percent: (100 * spend[budget].settled_usd) / spend[budget].limit_usd } : null).filter(Boolean);
  const closest = shares.sort((a, b) => b.percent - a.percent)[0];
  const marks = [...(spend.notice_percents ?? [50, 85, 90])].sort((a, b) => a - b);
  if (!closest || !marks.length || closest.percent < marks[0]) return null;
  return { ...closest, meter: Math.min(Math.round(closest.percent), 100), marks: marks.slice(1) };
}

export function isActiveRun(error) {
  return error instanceof ApiError && error.code === 'active_run';
}
