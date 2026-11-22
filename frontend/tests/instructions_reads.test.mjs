// Design review AH's scenarios (a retained callback starting a read under an earlier query: the personal instructions'
// size on Settings' General page): the real Settings dialog drawn on the stand-in (harness/standin.mjs) with the project
// prop the Shell gives (a record: known; undefined: open, its record not known; null: none open). The instructions
// endpoint is modelled as backend/app.py answers it (the file's text and hash; other_bytes: the named project's file;
// 404 for a project that is gone; a save refused with 409 settings_changed when the file changed). Every held answer is
// released by the scenario; no timer runs; no sleeps.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const KIB = 1024;
const P1 = { id: 'p1', name: 'Project Alpha', kind: 'research', sensitivity: 'normal' };
const P2 = { id: 'p2', name: 'Project Beta', kind: 'research', sensitivity: 'normal' };
const BIG = 'a'.repeat(13 * KIB); // with p1's 20 KiB file: over the cap; alone: under it
const CONFLICT = 'changed outside Scholia';
const OVER = 'Together the instructions are over 32 KiB';

function backend(state) {
  const asked = []; const paths = []; const held = new Set(state.held ?? []);
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const hash = () => `h${state.text.length}:${state.text.slice(0, 8)}`;
  const instructions = (path, init) => {
    const query = new URLSearchParams(path.split('?')[1]);
    if ((init.method ?? 'GET') === 'PUT') {
      const body = JSON.parse(init.body);
      if (body.hash != null && body.hash !== hash()) return [409, { code: 'settings_changed' }];
      state.text = body.text;
      return [200, { ok: true, warnings: [], hash: hash() }];
    }
    const beside = query.get('with_project');
    if (beside && state.gone?.includes(beside)) return [404, { code: 'not_found' }];
    if (query.get('project_id')) { // a project's own file, measured with the personal one
      return [200, { text: '', hash: 'p', other_bytes: state.text.length, unreadable: false, replaced: false, warnings: [], cap_bytes: 32 * KIB }];
    }
    return [200, { text: state.text, hash: hash(), other_bytes: beside ? state.files[beside] ?? 0 : 0, unreadable: false, replaced: false,
      warnings: [], cap_bytes: 32 * KIB }];
  };
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    paths.push(`${method} ${path}`);
    if (path === '/api/settings') return Promise.resolve(response(200, { values: { ui: { language: 'system', follow_up: 'steer' }, budget: { conversation_usd: 5 }, zotero: {}, limits: {} }, hash: 'h', problems: [] }));
    if (path.startsWith('/api/settings?project_id=')) return Promise.resolve(response(200, { values: { project: {}, limits: {} }, hash: 'h', problems: [] }));
    if (!path.startsWith('/api/instructions')) return new Promise(() => {});
    const kind = method === 'PUT' ? 'save' : 'read';
    const now = () => response(...instructions(path, init)); // answered as the file is when the answer is released
    if (!held.has(kind)) return Promise.resolve(now());
    return new Promise((resolve) => asked.push({ method, path, kind, answer: () => resolve(now()),
      fail: (code = 'internal', status = 500) => resolve(response(status, { code })) }));
  };
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.window = { addEventListener() {}, removeEventListener() {} }; // This project's folder field listens on it
  return { asked, paths, hold: (kind) => held.add(kind), free: (kind) => held.delete(kind),
    next: (test = () => true) => { const i = asked.findIndex(test); if (i < 0) throw new Error(`nothing asked: ${asked.map((r) => `${r.method} ${r.path}`).join(', ') || 'none held'}`); return asked.splice(i, 1)[0]; } };
}

async function world({ mount, jsx }, strict, more, project) {
  const state = { text: 'mine', files: { p1: 20 * KIB, p2: 5 * KIB }, ...more };
  const server = backend(state);
  const { Settings } = await load.component('Settings');
  const draw = (now) => jsx(Settings, { open: true, startPage: state.page, onOpenChange() {}, health: {}, onLanguage() {}, onProjectChanged() {}, ...now });
  const ui = await mount([draw({ project })], { strict }).settle();
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) {
      if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
    }
    return out.map((e) => ({ ...e, text: e.text.join(' ') })); };
  const act = async (fn) => { fn(); await ui.settle(); };
  const box = () => elements().filter((e) => e.props.id === (state.page === 'project' ? 'project-instructions' : 'personal-instructions'));
  const buttons = (label) => elements().filter((e) => e.type === 'ui:Button' && e.props.onClick && e.text.trim() === label);
  const s = {
    state, server,
    redraw: async (now) => { ui.show([draw(now)]); await ui.settle(); }, // the Shell drawing Settings again with the project it has now
    screen: () => ui.text(),
    size: () => /(\d+) of 32 KiB/.exec(ui.text())?.[1] ?? null, // the size said, or null when none is
    alerts: () => elements().filter((e) => e.props.role === 'alert').map((e) => e.text.trim()),
    text: () => box()[0]?.props.value ?? null,
    type: async (value) => { const [b] = box(); if (!b || b.props.readOnly) return false; await act(() => b.props.onChange({ target: { value } })); return true; },
    enabled: (label) => buttons(label).some((b) => !b.props.disabled),
    press: async (label) => { const [b] = buttons(label); if (!b || b.props.disabled) return false; await act(() => b.props.onClick({ preventDefault() {}, currentTarget: null })); return true; },
    release: (request, how = (r) => r.answer()) => act(() => how(request)),
    releaseAll: async (test = () => true, how = (r) => r.answer()) => { while (server.asked.some(test)) await s.release(server.next(test), how); },
    reads: () => server.paths.filter((p) => p.startsWith('GET /api/instructions')),
  };
  return s;
}
const saves = (r) => r.kind === 'save';
const reads = (r) => r.kind === 'read';

const scenarios = {
  // Recheck r11, finding 1 of both passes (a save that succeeds): Save is pressed while the open project's record is not
  // known; the record is known and the size is read with the project's file; the save answers. The size still counts
  // the project's file, the warning stays, and what was saved is read back.
  async aSaveAnsweredOnceTheRecordIsKnownCountsTheProjectsFile(w, strict) {
    const s = await world(w, strict, { held: ['save'] }, undefined);
    assert.equal(await s.type(BIG), true);
    assert.equal(await s.press('Save'), true);
    await s.redraw({ project: P1 });
    assert.equal(s.size(), '34', 'the size with the project\'s file, once its record is known');
    assert.ok(s.alerts().some((text) => text.includes(OVER)));
    await s.releaseAll(saves);
    assert.equal(s.size(), '34', 'the size leaves the project\'s file out after the save answered');
    assert.ok(s.alerts().some((text) => text.includes(OVER)), 'the warning is gone');
    assert.equal(s.text(), BIG);
    assert.equal(s.enabled('Save'), false, 'what was saved was not read back');
  },
  // The same when the save is refused because the file changed (409 settings_changed): the file is read again with the
  // project's file, the conflict is said, and the text typed stays.
  async aSaveRefusedOnceTheRecordIsKnownCountsTheProjectsFile(w, strict) {
    const s = await world(w, strict, { held: ['save'] }, undefined);
    assert.equal(await s.type(BIG), true);
    assert.equal(await s.press('Save'), true);
    await s.redraw({ project: P1 });
    s.state.text = 'theirs'; // changed by another program since it was read
    await s.releaseAll(saves);
    assert.equal(s.text(), BIG, 'the text typed is gone');
    assert.equal(s.size(), '34', 'the size leaves the project\'s file out after the save was refused');
    assert.ok(s.alerts().some((text) => text.includes(OVER)), 'the warning is gone');
    assert.ok(s.alerts().some((text) => text.includes(CONFLICT)), 'the conflict is not said');
  },
  // This review: the same callback under another change of what is open. Save is pressed with p1 open; p1 is known
  // deleted and p2 opens (the Shell gives its record, undefined, then p2's); the save answers. Nothing is asked for p1,
  // p2's file is counted, no failure is said, and what was saved is read back.
  async aSaveAnsweredAfterAnotherProjectOpenedAsksNothingOfTheFirst(w, strict) {
    const s = await world(w, strict, { held: ['save'] }, P1);
    assert.equal(s.size(), '21');
    assert.equal(await s.type('mine too'), true);
    assert.equal(await s.press('Save'), true);
    s.state.gone = ['p1'];
    await s.redraw({ project: undefined });
    await s.redraw({ project: P2 });
    assert.equal(s.size(), '6');
    const before = s.reads().length;
    await s.releaseAll(saves);
    assert.deepEqual(s.reads().slice(before).filter((p) => p.includes('p1')), [], 'a project that is gone was asked for');
    assert.deepEqual(s.alerts(), [], 'a failure is said');
    assert.equal(s.size(), '6');
    assert.equal(s.enabled('Save'), false, 'what was saved was not read back');
  },
  // This review: an earlier query's size is not said under another, whether the newer read is unanswered or failed. The
  // record is not known, then known; the read with the project's file is held, then fails.
  async anEarlierSizeIsNotSaidWhileTheProjectsIsNotRead(w, strict) {
    const s = await world(w, strict, {}, undefined);
    s.server.hold('read');
    await s.redraw({ project: P1 });
    assert.notEqual(s.size(), '1', 'the size without the project\'s file is said while the read with it is unanswered');
    await s.releaseAll(reads, (r) => r.fail());
    assert.notEqual(s.size(), '1', 'the size without the project\'s file is said after the read with it failed');
    assert.equal(s.alerts().length, 1, 'the failed read is not said');
    assert.equal(s.text(), 'mine', 'the editor is gone');
  },
  // This review (design X's rule at this reader): text typed is not replaced by a read the researcher did not ask for.
  // The file is changed by another program; the record becomes not known and known again. The text typed stays, and a
  // save is then refused and says so (ticket 14), the text still there.
  async textTypedStaysWhenTheRecordIsReadAgain(w, strict) {
    const s = await world(w, strict, {}, P1);
    assert.equal(await s.type('typed here'), true);
    s.state.text = 'theirs';
    await s.redraw({ project: undefined });
    assert.equal(s.text(), 'typed here', 'the text typed is replaced when the record is not known');
    await s.redraw({ project: P1 });
    assert.equal(s.text(), 'typed here', 'the text typed is replaced when the record is known again');
    assert.equal(await s.press('Save'), true);
    assert.equal(s.text(), 'typed here', 'the text typed is replaced by the refused save');
    assert.ok(s.alerts().some((text) => text.includes(CONFLICT)), 'the conflict is not said');
    assert.equal(s.state.text, 'theirs', 'the other program\'s text was overwritten');
  },
  // Point (c), for Harold: while the open project's record is not known no size is said (it would leave the project's
  // file out); the editor, its text and Save stay; the size is back once the record is known.
  async noSizeIsSaidWhileTheRecordIsNotKnown(w, strict) {
    const s = await world(w, strict, {}, P1);
    assert.equal(s.size(), '21');
    assert.equal(await s.type('typed here'), true);
    await s.redraw({ project: undefined });
    assert.equal(s.size(), null, `a size is said while the record is not known: ${s.size()} of 32 KiB`);
    assert.equal(s.text(), 'typed here');
    assert.equal(s.enabled('Save'), true);
    await s.redraw({ project: P1 });
    assert.equal(s.size(), '21');
    assert.equal(s.text(), 'typed here');
  },
};

// Green at the head: the behavior kept.
const guards = {
  async aSaveIsReadBackWithTheProjectsFile(w, strict) {
    const s = await world(w, strict, {}, P1);
    assert.equal(s.size(), '21');
    assert.equal(await s.type(BIG), true);
    assert.equal(s.size(), '34');
    assert.ok(s.alerts().some((text) => text.includes(OVER)));
    assert.equal(await s.press('Save'), true);
    assert.equal(s.state.text, BIG);
    assert.equal(s.size(), '34');
    assert.equal(s.enabled('Save'), false);
    assert.equal(await s.type('less'), true);
    assert.equal(s.size(), '21');
    assert.deepEqual(s.alerts(), []);
  },
  async aRefusedSaveKeepsTheTextAndSaysSo(w, strict) {
    const s = await world(w, strict, {}, P1);
    assert.equal(await s.type('typed here'), true);
    s.state.text = 'theirs';
    assert.equal(await s.press('Save'), true);
    assert.equal(s.text(), 'typed here');
    assert.ok(s.alerts().some((text) => text.includes(CONFLICT)));
    assert.equal(s.state.text, 'theirs');
    assert.equal(await s.press('Save'), true); // made again on what the file now holds
    assert.equal(s.state.text, 'typed here');
  },
  async withNoProjectOpenThePersonalFileIsCountedAlone(w, strict) {
    const s = await world(w, strict, {}, null);
    assert.equal(s.size(), '1');
    assert.equal(await s.type(BIG), true);
    assert.equal(s.size(), '13');
    assert.deepEqual(s.reads().filter((p) => p.includes('with_project')), []);
  },
  async aProjectOpenedCountsItsFile(w, strict) {
    const s = await world(w, strict, {}, null);
    assert.equal(await s.type('typed here'), true);
    await s.redraw({ project: P2 });
    assert.equal(s.size(), '6');
    assert.equal(s.text(), 'typed here');
    await s.redraw({ project: null });
    assert.equal(s.size(), '1');
    assert.equal(s.text(), 'typed here');
  },
  // AG's rider I, kept: a size asked for one project that answers after another opened is not said under it.
  async aLateSizeOfAnotherProjectIsNotSaid(w, strict) {
    const s = await world(w, strict, { held: ['read'] }, P1);
    await s.redraw({ project: undefined });
    await s.redraw({ project: P2 });
    await s.releaseAll((r) => reads(r) && !r.path.includes('with_project'));
    await s.releaseAll((r) => reads(r) && r.path.includes('with_project=p2'));
    assert.equal(s.size(), '6');
    await s.releaseAll(reads); // p1's, last
    assert.equal(s.size(), '6', 'the first project\'s file is counted under the second');
    assert.deepEqual(s.alerts(), []);
  },
  // This project's own editor (drawn for one project) counts the personal file with what is typed.
  async thisProjectsEditorCountsThePersonalFile(w, strict) {
    const s = await world(w, strict, { page: 'project', text: BIG }, P1);
    assert.equal(s.text(), '');
    assert.equal(s.size(), '13');
    assert.equal(await s.type('b'.repeat(20 * KIB)), true);
    assert.equal(s.size(), '34');
    assert.ok(s.alerts().some((text) => text.includes(OVER)));
  },
  async aFailedFirstReadIsSaidWithTryAgain(w, strict) {
    const s = await world(w, strict, { held: ['read'] }, P1);
    await s.releaseAll(reads, (r) => r.fail());
    assert.equal(s.text(), null);
    s.server.free('read');
    assert.equal(await s.press('Try again'), true);
    assert.equal(s.text(), 'mine');
    assert.equal(s.size(), '21');
  },
};

const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
for (const [name, run] of [...Object.entries(scenarios), ...Object.entries(guards)]) {
  if (only && !only.includes(name)) continue;
  for (const strict of [false, true]) {
    test(`${name}${strict ? ' (StrictMode)' : ''}`, async () => {
      const names = ['fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'window'];
      const kept = Object.fromEntries(names.map((n) => [n, Object.getOwnPropertyDescriptor(globalThis, n)]));
      try {
        await run(await load.fresh(), strict);
      } finally {
        for (const [n, d] of Object.entries(kept)) { if (d) Object.defineProperty(globalThis, n, d); else delete globalThis[n]; }
      }
    });
  }
}
