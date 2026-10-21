// A run's trace as Details shows it (S1-19b; slice-1 spec F11): read from GET /api/runs/{id}/trace a page
// at a time, and summed up from its events alone: the route and how it was decided, the retrieval, each
// step with its estimate and its attempts (each with the exact text it sent, in the content store), and
// the citation checks. Imported only by components/Trace.jsx, so it loads with Details.
import { get } from './api.js';

const PAGES = 50; // at most 10,000 events, far beyond a turn's

// Every event of the run, in order.
export async function readTrace(runId, read = get) {
  const events = [];
  let after = -1;
  for (let page = 0; page < PAGES; page += 1) {
    const found = await read(`/api/runs/${encodeURIComponent(runId)}/trace?after=${after}`);
    events.push(...found.events);
    if (found.next === null || found.next === undefined) break;
    after = found.next;
  }
  return events;
}

// { route, retrieval, search, steps: [{ phase, route, estimate, cut, attempts: [{ outcome, cost, charge, body, removed }] }],
//   checks, finished } from the trace's events. A step whose answer stopped at its output limit is cut.
export function summarize(events) {
  const found = { route: null, retrieval: null, search: null, steps: [], checks: [], finished: null };
  const steps = new Map();
  for (const { type, data, body_ref: body, body_removed: removed } of events) {
    if (type === 'route') {
      if (data.route) found.route = data.route;
      if (data.retrieval) found.retrieval = data.retrieval; // the router's decision, when it decided, comes later
    } else if (type === 'retrieval_trace') {
      found.search = { mode: data.mode, reason: data.reason ?? null, passages: (data.selected ?? []).length,
        rerank: data.rerank?.fallback ? 'fallback' : data.rerank ? 'reranked' : null };
    } else if (type === 'step_started') {
      const step = { phase: data.phase, route: data.route, estimate: data.estimate_usd ?? null, cut: false, attempts: [] };
      steps.set(data.step, step);
      found.steps.push(step);
    } else if (type === 'model_attempt') {
      steps.get(data.step)?.attempts.push({ outcome: data.outcome, cost: data.cost_usd ?? null, charge: data.charge ?? null,
        body: body ?? null, removed: Boolean(removed) }); // removed: its text went with a deleted paper
    } else if (type === 'step_finished') {
      const step = steps.get(data.step);
      if (step && data.cut) step.cut = true; // saved, and marked as stopped at its output limit
    } else if (type === 'citation_checks') {
      found.checks = data.citations ?? [];
    } else if (type === 'run_finished') {
      found.finished = data.status;
    }
  }
  return found;
}
