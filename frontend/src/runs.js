// Background runs as the interface follows them (backend/app.py /api/activity): a run started
// elsewhere, such as a full backup or an export, read until it ends.
import { get, post } from './api.js';

const POLL_MS = 700;

// The run's row in the background-run list once it has ended; onProgress(row) is called with each
// row read while it runs. A failed read is tried again at the next turn.
export async function followRun(runId, onProgress = () => {}, wait = (ms) => new Promise((r) => setTimeout(r, ms))) {
  for (;;) {
    let row = null;
    try {
      [row] = (await get(`/api/activity?run_id=${encodeURIComponent(runId)}`)).runs;
    } catch {
      row = null;
    }
    if (row && row.status !== 'running') return row;
    if (row) onProgress(row);
    await wait(POLL_MS);
  }
}

export const cancelRun = (runId) => post(`/api/runs/${encodeURIComponent(runId)}/cancel`);
export const retryRun = (runId) => post(`/api/runs/${encodeURIComponent(runId)}/retry`);

// What a finished run's row says as catalog keys: done, or why not (its reason's errors.* entry, a
// stop, or an interruption).
export function runOutcome(row) {
  if (row.status === 'succeeded') return { ok: true };
  if (row.status === 'cancelled') return { ok: false, key: row.cancel_reason === 'revoked' ? 'runs.revoked' : 'runs.stopped' };
  if (row.status === 'interrupted') return { ok: false, key: 'runs.interrupted' };
  return { ok: false, code: row.result?.reason ?? 'internal' };
}

// Progress as a fraction from 0 to 1, or null when the run reports none.
export function fraction(progress) {
  if (!progress || !(progress.total > 0)) return null;
  return Math.min(1, Math.max(0, progress.done / progress.total));
}
