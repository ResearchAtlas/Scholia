// Design review AG's scenarios (a read surviving its scope's invalidation: the audit log, the memory views): the real
// AuditLog and MemorySection drawn as Settings' Advanced page draws them, on the stand-in (harness/standin.mjs), with
// the project prop the Shell gives (a record: known; undefined: open, its record not known; null: none open). The
// audit endpoint is modelled as backend/app.py answers it. Every held answer is released by the scenario; no timer
// runs; no sleeps. Each asserts on what the drawn tree says and on what was asked.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const P1 = { id: 'p1', name: 'Project Alpha', kind: 'research', sensitivity: 'normal' };
const P2 = { id: 'p2', name: 'Project Beta', kind: 'research', sensitivity: 'normal' };
const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };

// The log: 120 rows, odd seqs project p1's, even ones p2's, each an allowed request to a destination naming its project.
const rows = () => Array.from({ length: 120 }, (_, i) => { const seq = 120 - i; const project = seq % 2 ? 'p1' : 'p2';
  return { seq, at: '2026-11-01T00:00:00Z', event: 'outbound', project_id: project,
    data: { decision: 'allow', kind: 'provider', destination: `dest-${project}-${seq}` } }; });

function backend(state) {
  const asked = []; const paths = []; const held = new Set(state.held ?? []);
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const audit = (path) => { // as backend/app.py audit_log
    const query = new URLSearchParams(path.split('?')[1]); const project = query.get('project_id');
    const before = query.get('before') ? Number(query.get('before')) : null; const limit = Number(query.get('limit') ?? 100);
    const found = state.log.filter((r) => (!project || r.project_id === project || r.event === 'audit_cleared') && (before === null || r.seq < before)).slice(0, limit);
    return { entries: structuredClone(found), next: found.length === limit ? found.at(-1).seq : null,
      allowed_off_this_mac: project ? state.log.filter((r) => r.project_id === project && r.event === 'outbound').length : null };
  };
  const memory = (path) => { const project = new URLSearchParams(path.split('?')[1]).get('project_id');
    return { personal: structuredClone(state.memory?.personal ?? []), project: structuredClone(project ? state.memory?.[project] ?? [] : []), memory_enabled: true }; };
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    paths.push(`${method} ${path}`);
    const kind = path.startsWith('/api/audit/export') ? 'export' : path.startsWith('/api/audit') ? (method === 'GET' ? 'audit' : 'clear')
      : path.startsWith('/api/memory') ? 'memory' : path === '/api/settings' ? 'settings' : path.startsWith('/api/instructions') ? 'instructions' : 'other';
    const now = () => ({ audit: () => audit(path), memory: () => memory(path),
      settings: () => ({ values: { ui: { language: 'system', follow_up: 'steer' }, budget: { conversation_usd: 5 }, zotero: {} }, hash: 'h', problems: [] }),
      // the personal instructions, measured with the project's file when one is named (backend/app.py get_instructions)
      instructions: () => ({ text: 'mine', hash: 'i', other_bytes: path.includes('with_project=') ? 20 * 1024 : 0, cap_bytes: 32 * 1024 }),
      export: () => ({ path: '/data/exports/audit-log-X.json', rows: 1 }) }[kind]?.() ?? { ok: true });
    if (kind === 'other') return new Promise(() => {});
    if (kind === 'clear' && !path.includes('token=')) return Promise.resolve(response(409, { code: 'confirmation_required', token: 't1' }));
    if (!held.has(kind)) return Promise.resolve(response(200, now()));
    return new Promise((resolve) => asked.push({ method, path, kind,
      answer: (body = now(), status = 200) => resolve(response(status, body)),
      fail: (code = 'internal', status = 500) => resolve(response(status, { code })) }));
  };
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  return { asked, paths, hold: (kind) => held.add(kind), free: (kind) => held.delete(kind),
    next: (test = () => true) => { const i = asked.findIndex(test); if (i < 0) throw new Error(`nothing asked: ${asked.map((r) => `${r.method} ${r.path}`).join(', ') || 'none held'}`); return asked.splice(i, 1)[0]; } };
}

async function world({ mount, jsx }, strict, state, file, name, props) {
  const server = backend(state);
  const part = (await load.component(file))[name];
  // the part alone, with the props Settings gives it; or as the scenario draws it (state.draw: the whole Settings dialog)
  const draw = (now) => (state.draw ? state.draw(jsx, part, now) : jsx(part, now));
  const ui = await mount([draw(props)], { strict }).settle();
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) {
      if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
    }
    return out.map((e) => ({ ...e, text: e.text.join(' ') })); };
  const hidden = (e) => [e, ...e.up].some((x) => x.type === 'ui:Dialog' && !x.props.open);
  const shown = () => elements().filter((e) => !hidden(e));
  const act = async (fn) => { fn(); await ui.settle(); };
  const buttons = (label) => shown().filter((e) => (e.type === 'ui:Button' || e.type === 'button') && e.props.onClick && e.text.trim() === label);
  const s = {
    ui, server, shown, act,
    redraw: async (now) => { ui.show([draw(now)]); await ui.settle(); }, // the Shell drawing Settings again with the project it has now
    screen: () => shown().filter((e) => !e.up.length).map((e) => e.text).join(' '),
    has: (label) => buttons(label).length > 0,
    enabled: (label) => !one(buttons(label), label).props.disabled,
    // presses as the researcher can: a control that is not drawn or is disabled takes no press; whether it took it
    press: async (label) => { const [b] = buttons(label); if (!b || b.props.disabled) return false; await act(() => b.props.onClick({ preventDefault() {}, currentTarget: null })); return true; },
    chosen: () => shown().filter((e) => e.props.role === 'radio' && e.props['aria-checked']).map((e) => e.text.trim()),
    choose: async (label) => { const [r] = shown().filter((e) => e.props.role === 'radio' && e.text.trim() === label); if (!r || r.props.disabled) return false; await act(() => r.props.onClick()); return true; },
    says: (role) => shown().filter((e) => e.props.role === role).map((e) => e.text.trim()),
    rows: () => shown().filter((e) => e.type === 'li').map((e) => e.text),
    of: (project) => s.rows().filter((text) => text.includes(`dest-${project}-`)).length,
    release: (request, how = (r) => r.answer()) => act(() => how(request)),
    releaseAll: async (test = () => true, how = (r) => r.answer()) => { while (server.asked.some(test)) await s.release(server.next(test), how); },
    unscoped: () => server.paths.filter((p) => p.startsWith('GET /api/audit') && !p.includes('project_id')),
  };
  return s;
}
const auditWorld = (w, strict, state, project) => world(w, strict, Object.assign(state, { log: state.log ?? rows() }), 'Governance', 'AuditLog', { project }); // the scenario's own state: it may change the log
const record = (id, content, status = 'confirmed') => ({ id, content, type: 'fact', status, version: 1, sources: [], valid_from: null, structured: {} });
const memoryState = (more = {}) => ({ log: [], memory: { personal: [record('m0', 'Fact Personal')], p1: [record('m1', 'Fact Alpha')], p2: [record('m2', 'Fact Beta')] }, ...more });
const audits = (r) => r.kind === 'audit';
const memories = (r) => r.kind === 'memory';

const scenarios = {
  // The connector's P2 and recheck r10's finding 1 (both passes): a project's log is asked; its record becomes not
  // known (the projects are read again); the answer comes. Nothing of it is shown, no older page can be asked for, and
  // nothing is asked for no project.
  async aLogAskedBeforeTheRecordWasNotKnownIsNotShown(w, strict) {
    const s = await auditWorld(w, strict, { held: ['audit'] }, P1);
    await s.redraw({ project: undefined });
    await s.releaseAll(audits);
    assert.equal(s.rows().length, 0, 'the page asked before is shown while the record is not known');
    if (await s.press('Show older')) await s.releaseAll(audits);
    assert.deepEqual(s.unscoped(), [], 'the whole log was read while "This project" is the choice');
    assert.equal(s.of('p2'), 0, 'another project\'s entries are shown');
    await s.redraw({ project: P1 });
    await s.releaseAll(audits);
    assert.equal(s.of('p1'), 50, 'its own log is read once the record is known');
    assert.equal(s.of('p2'), 0);
  },
  // This review: Export pressed under Everything; This project chosen while the export is written; the export answers,
  // and the read that follows it is the one kept from Everything. What is shown under This project is this project's.
  async aReadAfterAnExportIsOfTheScopeChosenNow(w, strict) {
    const s = await auditWorld(w, strict, { held: ['export'] }, P1);
    assert.equal(await s.choose('Everything'), true);
    assert.ok(s.of('p2') > 0, 'Everything shows the whole log');
    assert.equal(await s.press('Export'), true);
    assert.equal(await s.choose('This project'), true);
    assert.equal(s.of('p2'), 0);
    await s.releaseAll((r) => r.kind === 'export');
    assert.deepEqual(s.chosen(), ['This project']);
    assert.equal(s.of('p2'), 0, 'another project\'s entries are shown under This project');
    assert.ok(s.screen().includes('allowed to leave this Mac'), 'the project\'s count is gone');
    if (await s.press('Show older')) assert.equal(s.of('p2'), 0, 'an older page of the whole log was added under This project');
  },
  // The same the other way: Export pressed under This project, Everything chosen meanwhile: Everything shows the whole log.
  async aReadAfterAnExportUnderThisProjectLeavesEverythingWhole(w, strict) {
    const s = await auditWorld(w, strict, { held: ['export'] }, P1);
    assert.equal(await s.press('Export'), true);
    assert.equal(await s.choose('Everything'), true);
    await s.releaseAll((r) => r.kind === 'export');
    assert.deepEqual(s.chosen(), ['Everything']);
    assert.ok(s.of('p2') > 0, 'Everything shows only this project\'s entries');
  },
  // This review: a scope's read failed; another scope is chosen (or the record becomes not known): the failure said is
  // not the earlier scope's, and no Try again is offered that reads nothing.
  async anEarlierScopesFailureIsNotSaidUnderAnother(w, strict) {
    const s = await auditWorld(w, strict, { held: ['audit'] }, P1);
    await s.releaseAll(audits, (r) => r.fail());
    assert.equal(s.says('alert').length, 1, 'the failed read is said');
    assert.equal(await s.choose('Everything'), true);
    assert.deepEqual(s.says('alert'), [], 'This project\'s failure is said while Everything is read');
    await s.releaseAll(audits);
    assert.equal(await s.choose('This project'), true);
    await s.releaseAll(audits, (r) => r.fail());
    await s.redraw({ project: undefined });
    assert.deepEqual(s.says('alert'), [], 'a failure with a Try again that reads nothing is said while the record is not known');
  },
  // This review: the log is cleared and the read after it fails: nothing read before the clearing stays on screen,
  // and the failure is said.
  async aClearedLogShowsNothingReadBeforeIt(w, strict) {
    const state = { log: rows() };
    const s = await auditWorld(w, strict, state, P1);
    assert.equal(s.of('p1'), 50);
    assert.equal(await s.press('Clear the log'), true);
    s.server.hold('audit');
    state.log = [{ seq: 121, at: '2026-11-02T00:00:00Z', event: 'audit_cleared', project_id: null, data: { rows: 120 } }];
    assert.equal(await s.press('Clear'), true);
    assert.equal(s.of('p1'), 0, 'entries read before the clearing are shown after it');
    await s.releaseAll(audits, (r) => r.fail());
    assert.equal(s.of('p1'), 0, 'entries read before the clearing stay when the read after it fails');
    assert.equal(s.says('alert').length, 1, 'the failed read is not said');
  },
  // This review: one project's memory is never shown as another's. The open project changes under the open Settings
  // (a project made while Settings is open: the Shell gives the old record, undefined, then the new one); the first
  // project's read answers last.
  async oneProjectsMemoryIsNotShownAsAnothers(w, strict) {
    const s = await world(w, strict, memoryState({ held: ['memory'] }), 'Memory', 'MemorySection', { project: P1 });
    await s.redraw({ project: undefined });
    await s.redraw({ project: P2 });
    await s.releaseAll((r) => memories(r) && r.path.includes('project_id=p2'));
    assert.ok(s.screen().includes('Fact Beta'));
    await s.releaseAll((r) => memories(r) && !r.path.includes('project_id'));
    await s.releaseAll(memories); // the first project's, last
    assert.ok(!s.screen().includes('Fact Alpha'), 'the first project\'s memory is shown as the second\'s');
    assert.ok(s.screen().includes('Fact Beta'));
  },
  // This review: the open project is known deleted (none is open for a moment: null), then another is opened; the read
  // asked while none was open answers last. The project now open shows its own memory.
  async memoryReadWhileNoProjectWasOpenIsNotShownUnderTheNext(w, strict) {
    const s = await world(w, strict, memoryState(), 'Memory', 'MemorySection', { project: P1 });
    s.server.hold('memory');
    await s.redraw({ project: null });
    await s.redraw({ project: P2 });
    await s.releaseAll((r) => memories(r) && r.path.includes('project_id=p2'));
    await s.releaseAll(memories);
    assert.ok(s.screen().includes('Fact Beta'), 'the open project\'s memory is not shown: a read for no project answered last');
  },
  // Recheck r10 pass 1, finding 2: while the open project's record is not known, Everything can be chosen, read and
  // exported (it needs no project); This project reads and exports nothing meanwhile.
  async everythingIsReadWhileTheRecordIsNotKnown(w, strict) {
    const s = await auditWorld(w, strict, {}, undefined);
    assert.deepEqual(s.server.paths.filter((p) => p.includes('/api/audit')), [], 'asked before a choice');
    assert.equal(s.enabled('Export'), false);
    assert.equal(await s.choose('Everything'), true, 'Everything cannot be chosen while the record is not known');
    assert.ok(s.of('p1') > 0 && s.of('p2') > 0, 'the whole log is not shown');
    assert.equal(s.enabled('Export'), true);
    assert.equal(await s.choose('This project'), true);
    assert.equal(s.rows().length, 0, 'the whole log stays under This project');
    assert.equal(s.enabled('Export'), false);
    await s.redraw({ project: P1 });
    assert.equal(s.of('p1'), 50);
    assert.equal(s.of('p2'), 0);
  },
  // This review: the memory views. The record becomes not known and known again; the two reads answer in the other
  // order. The project's memory is shown, and nothing was read for no project while one is open.
  async memoryReadWithoutTheProjectIsNotShownOnceItIsKnown(w, strict) {
    const s = await world(w, strict, memoryState(), 'Memory', 'MemorySection', { project: P1 });
    assert.ok(s.screen().includes('Fact Alpha'));
    s.server.hold('memory');
    await s.redraw({ project: undefined });
    await s.redraw({ project: P1 });
    const late = s.server.asked.filter((r) => memories(r) && !r.path.includes('project_id'));
    await s.releaseAll((r) => memories(r) && r.path.includes('project_id'));
    await s.releaseAll(memories);
    assert.ok(s.screen().includes('Fact Alpha'), 'the project\'s memory is not shown: a read for no project answered last');
    assert.equal(late.length, 0, 'memory was read for no project while one is open');
  },
  // This review: a clearing whose answer is lost (the engine cleared the log): it may have happened, so nothing read
  // before it stays; the log is read anew, and the failure is said.
  async aClearingWhoseAnswerIsLostShowsNothingReadBeforeIt(w, strict) {
    const state = { log: rows(), held: ['clear'] };
    const s = await auditWorld(w, strict, state, P1);
    assert.equal(await s.press('Clear the log'), true);
    assert.equal(await s.press('Clear'), true);
    state.log = [{ seq: 121, at: '2026-11-02T00:00:00Z', event: 'audit_cleared', project_id: null, data: { rows: 120 } }];
    await s.releaseAll((r) => r.kind === 'clear', (r) => r.fail('unavailable', 503));
    assert.equal(s.of('p1'), 0, 'entries read before a clearing that happened stay on screen');
    assert.equal(s.rows().length, 1, 'the cleared log is not read');
    assert.equal(s.says('alert').length, 1, 'the failed request is not said');
  },
};

// Green at the head: the behavior kept.
const guards = {
  async aProjectsLogIsReadAndPaged(w, strict) {
    const s = await auditWorld(w, strict, {}, P1);
    assert.deepEqual(s.chosen(), ['This project']);
    assert.equal(s.of('p1'), 50);
    assert.equal(s.of('p2'), 0);
    assert.equal(await s.press('Show older'), true);
    assert.equal(s.of('p1'), 60);
    assert.equal(s.of('p2'), 0);
    assert.equal(s.has('Show older'), false);
    assert.deepEqual(s.unscoped(), []);
    assert.ok(s.screen().includes('60 requests from this project'));
  },
  async everythingIsTheWholeLogAndPaged(w, strict) {
    const s = await auditWorld(w, strict, {}, P1);
    assert.equal(await s.choose('Everything'), true);
    assert.equal(s.rows().length, 50);
    assert.equal(await s.press('Show older'), true);
    assert.equal(s.rows().length, 100);
    assert.ok(!s.screen().includes('allowed to leave this Mac'));
  },
  async anExportSaysItsFileAndTheLogIsReadAgain(w, strict) {
    const s = await auditWorld(w, strict, {}, P1);
    const before = s.server.paths.filter((p) => p.startsWith('GET /api/audit')).length;
    assert.equal(await s.press('Export'), true);
    assert.ok(s.says('status').some((text) => text.includes('/data/exports/audit-log-X.json')));
    assert.ok(s.server.paths.includes('POST /api/audit/export'));
    assert.ok(s.server.paths.filter((p) => p.startsWith('GET /api/audit')).length > before, 'the log is read again after the export');
    assert.equal(s.of('p1'), 50);
  },
  async aLateOlderPageIsNotAddedToAnotherLog(w, strict) {
    const s = await auditWorld(w, strict, {}, P1);
    s.server.hold('audit');
    assert.equal(await s.press('Show older'), true);
    const older = s.server.next(audits);
    s.server.free('audit');
    assert.equal(await s.choose('Everything'), true);
    await s.release(older);
    assert.equal(s.rows().length, 50, 'This project\'s older page was added to Everything');
  },
  async noProjectOpenReadsTheWholeLog(w, strict) {
    const s = await auditWorld(w, strict, {}, null);
    assert.equal(s.rows().length, 50);
    assert.deepEqual(s.chosen(), [], 'a choice is drawn with no project open');
    await s.redraw({ project: P1 });
    assert.deepEqual(s.chosen(), ['Everything']);
  },
  async aProjectClosedReadsTheWholeLog(w, strict) {
    const s = await auditWorld(w, strict, {}, P1);
    await s.redraw({ project: null });
    assert.equal(s.rows().length, 50);
    assert.ok(s.of('p2') > 0);
    assert.equal(s.enabled('Export'), true);
  },
  async aProjectsMemoryIsShown(w, strict) {
    const s = await world(w, strict, memoryState(), 'Memory', 'MemorySection', { project: P1 });
    assert.ok(s.screen().includes('Fact Alpha') && s.screen().includes('Fact Personal'));
    await s.redraw({ project: null });
    assert.ok(s.screen().includes('Fact Personal') && !s.screen().includes('Fact Alpha'), 'with no project open: personal memory alone');
  },
};

const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
for (const [name, run] of [...Object.entries(scenarios), ...Object.entries(guards)]) {
  if (only && !only.includes(name)) continue;
  for (const strict of [false, true]) {
    test(`${name}${strict ? ' (StrictMode)' : ''}`, async () => {
      const names = ['fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'];
      const kept = Object.fromEntries(names.map((n) => [n, Object.getOwnPropertyDescriptor(globalThis, n)]));
      try {
        await run(await load.fresh(), strict);
      } finally {
        for (const [n, d] of Object.entries(kept)) { if (d) Object.defineProperty(globalThis, n, d); else delete globalThis[n]; }
      }
    });
  }
}
