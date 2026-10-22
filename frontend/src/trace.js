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

// { route, retrieval, search, steps: [{ phase, route, estimate, funded, cut, attempts: [{ outcome, cost, charge, body, removed, unavailable }],
//   tools: [{ name, ok, error }] }], skills, checks, finished } from the trace's events. A step whose answer stopped at its output
// limit is cut. S1-19c: each step's tool calls, by name and outcome, and the skills used (never their text).
export function summarize(events) {
  const found = { route: null, retrieval: null, search: null, steps: [], checks: [], pairs: null, historyLeftOut: 0, finished: null,
    skills: [], memory: 0 };
  const calls = new Map(); // call id -> its entry in its step's tools
  const steps = new Map();
  for (const { type, data, body_ref: body, body_removed: removed } of events) {
    if (type === 'route') {
      if (data.route) found.route = data.route;
      if (data.retrieval) found.retrieval = data.retrieval; // the router's decision, when it decided, comes later
    } else if (type === 'retrieval_trace' && data.memory) { // S1-21a: the memory records the request held, by id
      found.memory = (data.memory.records ?? []).length + (data.memory.state ?? []).length;
      found.memoryMode = data.memory.mode ?? null; // ranked by meaning (similarity), or the newest (recency)
    } else if (type === 'retrieval_trace' && !data.tool_call_id) { // the router's pre-load; a tool's is under its call
      found.search = { mode: data.mode, reason: data.reason ?? null, passages: (data.selected ?? []).length,
        rerank: data.rerank?.fallback ? 'fallback' : data.rerank ? 'reranked' : null };
    } else if (type === 'step_started') {
      const step = { phase: data.phase, route: data.route, estimate: data.estimate_usd ?? null,
        funded: data.funded ?? null, cut: false, attempts: [], tools: [] }; // funded: what paid a judge call (share, answer_unspent, budget)
      if (data.history_left_out) found.historyLeftOut = data.history_left_out; // left out: a paper it drew on is deleted, or its project no longer allows this request
      steps.set(data.step, step);
      found.steps.push(step);
    } else if (type === 'model_attempt') {
      steps.get(data.step)?.attempts.push({ outcome: data.outcome, cost: data.cost_usd ?? null, charge: data.charge ?? null,
        body: body ?? null, removed: Boolean(removed), // removed: its text went with a deleted paper
        unavailable: Boolean(data.body_unavailable) }); // the text could not be kept when it was sent
    } else if (type === 'step_finished') {
      const step = steps.get(data.step);
      if (step && data.cut) step.cut = true; // saved, and marked as stopped at its output limit
    } else if (type === 'tool_call_started') {
      const call = { name: data.name ?? null, ok: null, error: null };
      calls.set(data.call_id, call);
      steps.get(data.step)?.tools.push(call);
    } else if (type === 'tool_result') {
      const call = calls.get(data.call_id);
      if (call) Object.assign(call, { ok: Boolean(data.ok), error: data.error ?? null });
    } else if (type === 'skill_activated') {
      if (!found.skills.includes(data.name)) found.skills.push(data.name);
    } else if (type === 'citation_checks') {
      found.checks = data.citations ?? [];
      found.pairs = data.pairs ?? null; // { judged, failed, budget }: every pair's outcome, counted
    } else if (type === 'run_finished') {
      found.finished = data.status;
    }
  }
  return found;
}
