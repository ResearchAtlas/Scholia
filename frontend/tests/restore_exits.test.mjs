// Design review AB: a restore that succeeded replaced the database under everything the window holds, so every way of
// leaving its result reloads the window. The real Settings (its Advanced page, with the real BackupsSection) and the real
// DamagedDatabaseScreen on the stand-in (harness/standin.mjs). Every answer is released by the scenario, no timer runs,
// nothing sleeps. Each asserts on how many times the window was asked to reload.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const DONE = { ok: true, not_recorded: [], missing_files: [] };
async function world({ mount, jsx }, strict, { damaged = false } = {}) {
  const asked = []; const seen = { reloads: 0, restored: 0, health: 200 };
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    if (method === 'GET' && path === '/api/settings') return Promise.resolve(response(200, { hash: 'h', values: { models: {}, limits: {}, ui: { language: 'en' }, budget: {}, zotero: {}, subagents: { models: [] } } }));
    if (method === 'GET' && path === '/api/activity') return Promise.resolve(response(200, { runs: [] }));
    if (method === 'GET' && path === '/api/backups') return Promise.resolve(response(200, { backups: [{ id: 'daily/b1', kind: 'daily', time: '2026-01-01T00:00:00Z', size: 1000 }] }));
    if (method === 'GET' && path === '/api/health') return seen.health === 0 ? Promise.reject(new TypeError('Failed to fetch')) : Promise.resolve(response(200, { ok: true }));
    if (method !== 'POST' || path !== '/api/backups/restore') return new Promise(() => {});
    return new Promise((resolve, reject) => asked.push({ answer: (body = DONE) => resolve(response(200, body)),
      fail: (code, status) => (status === 0 ? reject(new TypeError('Failed to fetch')) : resolve(response(status, { code }))) }));
  };
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400, location: { reload() { seen.reloads += 1; } } };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  let ui;
  const settings = async (open) => { const { Settings } = await load.component('Settings');
    return [jsx(Settings, { open, onOpenChange: (next) => { settings(next).then((drawn) => ui.show(drawn)); }, health: {},
      project: { id: 'p1', name: 'Project', kind: 'research' }, onLanguage() {}, startPage: 'advanced' })]; };
  // onRestored as App.jsx gave it before design review AB (the app's own load, which reloads nothing): the screen no
  // longer takes it, and a scenario fails if it comes back and is called
  const screen = async () => { const { DamagedDatabaseScreen } = await load.component('Backups');
    return [jsx(DamagedDatabaseScreen, { onRestored: () => { seen.restored += 1; }, restart: false })]; };
  ui = await mount(await (damaged ? screen() : settings(true)), { strict }).settle();
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) {
      if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
    }
    return out.map((e) => ({ ...e, text: e.text.join(' ').trim() })); };
  const hidden = (e) => [e, ...e.up].some((x) => x.type === 'ui:Dialog' && !x.props.open);
  const act = async (fn) => { fn(); await ui.settle(); };
  const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
  const button = (label, inside) => one(elements().filter((e) => e.type === 'ui:Button' && !hidden(e) && e.text === label
    && (!inside || e.up.some((x) => x.type === inside))), label);
  const api = { seen, asked, ui, act,
    says: (text) => elements().some((e) => !hidden(e) && e.text.includes(text)),
    // Restore on the backup's row, then Restore in the confirmation: the request is under way
    restore: async () => { await act(() => button('Restore…', 'li').props.onClick()); await act(() => button('Restore', 'ui:DialogFooter').props.onClick()); },
    answer: (how = (r) => r.answer()) => act(() => how(asked.shift())),
    press: (label) => act(() => button(label).props.onClick()),
    // the Settings dialog closed as the researcher closes it (Escape, its close button, a click outside)
    close: async () => { await act(() => one(elements().filter((e) => e.type === 'ui:Dialog' && !e.up.some((x) => x.type === 'ui:Dialog')), 'Settings').props.onOpenChange(false)); await ui.settle(); },
    page: (name) => act(() => one(elements().filter((e) => e.type === 'button' && e.text === name), name).props.onClick()),
    // the window replaced by another screen (App.jsx: the app found limited): everything drawn goes
    replaced: () => act(() => ui.show([])),
    cancel: () => act(() => button('Cancel', 'ui:DialogFooter').props.onClick()),
  };
  return api;
}

const scenarios = {
  // Guard: Continue reloads the window, once.
  async continueAfterARestoreReloadsTheWindow(w, strict) {
    const s = await world(w, strict);
    await s.restore(); await s.answer();
    assert.ok(s.says('Restored.'), 'the result is shown');
    assert.equal(s.seen.reloads, 0, 'before it is left');
    await s.press('Continue');
    assert.equal(s.seen.reloads, 1);
  },
  // r5-1's second path: Settings closed without Continue.
  async settingsClosedAfterARestoreReloadsTheWindow(w, strict) {
    const s = await world(w, strict);
    await s.restore(); await s.answer();
    await s.close();
    assert.equal(s.seen.reloads, 1);
  },
  // This review: another Settings page chosen takes the result, and its Continue, away.
  async anotherSettingsPageAfterARestoreReloadsTheWindow(w, strict) {
    const s = await world(w, strict);
    await s.restore(); await s.answer();
    await s.page('General');
    assert.equal(s.seen.reloads, 1);
  },
  // This review: the window replaced by another screen while the result is shown.
  async theWindowReplacedAfterARestoreReloadsIt(w, strict) {
    const s = await world(w, strict);
    await s.restore(); await s.answer();
    await s.replaced();
    assert.equal(s.seen.reloads, 1);
  },
  // This review: the answer comes after its screen went (the app was found limited while the restore was under way).
  async aRestoreAnsweredAfterItsScreenWentReloadsTheWindow(w, strict) {
    const s = await world(w, strict);
    await s.restore();
    await s.replaced();
    assert.equal(s.seen.reloads, 0, 'nothing is known yet');
    await s.answer();
    assert.equal(s.seen.reloads, 1);
  },
  // r5-1's first path: Continue on the damaged-database screen.
  async continueOnTheDamagedScreenReloadsTheWindow(w, strict) {
    const s = await world(w, strict, { damaged: true });
    await s.restore(); await s.answer();
    await s.press('Continue');
    assert.equal(s.seen.reloads, 1);
    assert.equal(s.seen.restored, 0, 'the app drawn again in the same window');
  },
  // Rider U: a restore that got no answer may have happened (the backend ends it whatever becomes of the request): the
  // engine is asked, and if it answers the window reloads; if it does not, the dialog says so and nothing reloads.
  async aRestoreThatGotNoAnswerReloadsTheWindowIfTheEngineAnswers(w, strict) {
    const s = await world(w, strict);
    await s.restore(); await s.answer((r) => r.fail(null, 0));
    assert.equal(s.seen.reloads, 1);
  },
  async aRestoreThatGotNoAnswerFromAnEngineThatIsGoneSaysSo(w, strict) {
    const s = await world(w, strict);
    s.seen.health = 0;
    await s.restore(); await s.answer((r) => r.fail(null, 0));
    assert.equal(s.seen.reloads, 0);
    assert.ok(s.says('not responding'));
  },
  // Guards: a restore the backend refused changed nothing, and nothing reloads, then or when Settings is closed; nor
  // when Settings is closed with no restore at all.
  async aRefusedRestoreReloadsNothing(w, strict) {
    const s = await world(w, strict);
    await s.restore(); await s.answer((r) => r.fail('work_running', 409));
    assert.ok(s.says('did not stop in time'));
    await s.cancel(); await s.page('General'); await s.close();
    assert.equal(s.seen.reloads, 0);
  },
  async settingsClosedWithNoRestoreReloadsNothing(w, strict) {
    const s = await world(w, strict);
    await s.page('General'); await s.page('Advanced'); await s.close(); await s.replaced();
    assert.equal(s.seen.reloads, 0);
  },
};

const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
for (const [name, run] of Object.entries(scenarios)) {
  if (only && !only.includes(name)) continue;
  for (const strict of [false, true]) {
    test(`${name}${strict ? ' (StrictMode)' : ''}`, async () => {
      const names = ['fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'window', 'document'];
      const kept = Object.fromEntries(names.map((n) => [n, Object.getOwnPropertyDescriptor(globalThis, n)]));
      try {
        await run(await load.fresh(), strict);
      } finally {
        for (const [n, d] of Object.entries(kept)) { if (d) Object.defineProperty(globalThis, n, d); else delete globalThis[n]; }
      }
    });
  }
}
