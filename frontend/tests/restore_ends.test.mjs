// Design review AC: every way a restore's request can end, and a window loaded while a restore is under way. The real
// Settings (its Advanced page, with the real BackupsSection) and the real App on the stand-in (harness/standin.mjs).
// Every answer is released by the scenario, no timer runs, nothing sleeps.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const DONE = { ok: true, not_recorded: [], missing_files: [] };
const SETTINGS = { hash: 'h', values: { models: {}, limits: {}, ui: { language: 'en' }, budget: {}, zotero: {}, subagents: { models: [] } } };
const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
const unreadable = (status) => ({ ok: status < 400, status, json: async () => { throw new TypeError('the body was lost'); } });
const unanswered = () => Promise.reject(new TypeError('Failed to fetch'));

function elementsOf(ui) {
  const out = []; const stack = [];
  for (const h of ui.hosts()) {
    if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
    else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
  }
  return out.map((e) => ({ ...e, text: e.text.join(' ').trim() }));
}
const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
function globals(seen) {
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400, location: { reload() { seen.reloads += 1; } },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null,
    documentElement: { classList: { toggle() {} } } };
}

// Settings open at Advanced, one automatic backup listed; health answers as seen.health says (0: no answer).
async function settingsWorld({ mount, jsx }, strict) {
  const asked = []; const seen = { reloads: 0, health: 200, healthAsked: 0 };
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    if (method === 'GET' && path === '/api/settings') return Promise.resolve(response(200, SETTINGS));
    if (method === 'GET' && path === '/api/activity') return Promise.resolve(response(200, { runs: [] }));
    if (method === 'GET' && path === '/api/backups') return Promise.resolve(response(200, { backups: [{ id: 'daily/b1', kind: 'daily', time: '2026-01-01T00:00:00Z', size: 1000 }] }));
    if (method === 'GET' && path === '/api/health') { seen.healthAsked += 1; return seen.health === 0 ? unanswered() : Promise.resolve(response(200, { ok: true })); }
    if (method !== 'POST' || path !== '/api/backups/restore') return new Promise(() => {});
    return new Promise((resolve, reject) => asked.push({ resolve, reject }));
  };
  globals(seen);
  let ui;
  const settings = async (open) => { const { Settings } = await load.component('Settings');
    return [jsx(Settings, { open, onOpenChange: (next) => { settings(next).then((drawn) => ui.show(drawn)); }, health: {},
      project: { id: 'p1', name: 'Project', kind: 'research' }, onLanguage() {}, startPage: 'advanced' })]; };
  ui = await mount(await settings(true), { strict }).settle();
  const elements = () => elementsOf(ui);
  const hidden = (e) => [e, ...e.up].some((x) => x.type === 'ui:Dialog' && !x.props.open);
  const act = async (fn) => { fn(); await ui.settle(); };
  const button = (label, inside) => one(elements().filter((e) => e.type === 'ui:Button' && !hidden(e) && e.text === label
    && (!inside || e.up.some((x) => x.type === inside))), label);
  return { seen,
    says: (text) => elements().some((e) => !hidden(e) && e.text.includes(text)),
    restore: async () => { await act(() => button('Restore…', 'li').props.onClick()); await act(() => button('Restore', 'ui:DialogFooter').props.onClick()); },
    again: () => act(() => button('Restore', 'ui:DialogFooter').props.onClick()),
    ends: (how) => act(() => how(asked.shift())),
    close: async () => { await act(() => one(elements().filter((e) => e.type === 'ui:Dialog' && !e.up.some((x) => x.type === 'ui:Dialog')), 'Settings').props.onOpenChange(false)); await ui.settle(); },
    page: (name) => act(() => one(elements().filter((e) => e.type === 'button' && e.text === name), name).props.onClick()),
    replaced: () => act(() => ui.show([])),
    cancel: () => act(() => button('Cancel', 'ui:DialogFooter').props.onClick()),
  };
}

// The app at launch: health answers what the scenario gives it; every other request is recorded, the two launch reads
// answered, the rest never.
async function appWorld({ mount, jsx }, strict, health) {
  const seen = { reloads: 0, asked: [], health };
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    seen.asked.push(`${method} ${path}`);
    if (method === 'GET' && path === '/api/health') return Promise.resolve(response(200, seen.health));
    if (method === 'GET' && path === '/api/setup') return Promise.resolve(response(200, { needed: false }));
    if (method === 'GET' && path === '/api/settings') return Promise.resolve(response(200, SETTINGS));
    return new Promise(() => {});
  };
  globals(seen);
  const { App } = await load.component('../App');
  const ui = await mount([jsx(App, {})], { strict }).settle();
  return { seen,
    says: (text) => elementsOf(ui).some((e) => e.text.includes(text)),
    reads: () => seen.asked.filter((request) => request !== 'GET /api/health'),
    press: async (label) => { one(elementsOf(ui).filter((e) => e.type === 'ui:Button' && e.text === label), label).props.onClick(); await ui.settle(); },
  };
}

const scenarios = {
  // r6-1's finding 1: the engine answered 200, so the restore happened; the answer's body was lost.
  async aRestoreAnsweredWithABodyThatCannotBeReadReloadsTheWindow(w, strict) {
    const s = await settingsWorld(w, strict);
    await s.restore(); await s.ends((r) => r.resolve(unreadable(200)));
    assert.equal(s.seen.healthAsked, 1, 'the engine is asked');
    assert.equal(s.seen.reloads, 1);
  },
  // This review: an answer that is neither a result nor a refusal the engine stated (an error with no code).
  async aRestoreAnsweredWithAnErrorTheEngineDidNotStateReloadsTheWindow(w, strict) {
    const s = await settingsWorld(w, strict);
    await s.restore(); await s.ends((r) => r.resolve(unreadable(500)));
    assert.equal(s.seen.reloads, 1);
  },
  // This review: a 200 that is not a result (nothing in the engine sends one; the rule is by what was said).
  async aRestoreAnsweredWithA200ThatIsNoResultReloadsTheWindow(w, strict) {
    const s = await settingsWorld(w, strict);
    await s.restore(); await s.ends((r) => r.resolve(response(200, {})));
    assert.equal(s.seen.reloads, 1);
    assert.ok(!s.says('Restored.'));
  },
  // This review, rider R2: no answer, and health got none either. The restore may still have happened or be under way
  // (whatever lost one answer can lose the next): the dialog says so, as today, and leaving reloads.
  async aRestoreThatGotNoAnswerFromAnEngineThatDoesNotAnswerReloadsWhenSettingsIsClosed(w, strict) {
    const s = await settingsWorld(w, strict);
    s.seen.health = 0;
    await s.restore(); await s.ends((r) => r.reject(new TypeError('Failed to fetch')));
    assert.ok(s.says('not responding'));
    assert.equal(s.seen.reloads, 0, 'not before it is left');
    await s.cancel(); await s.close();
    assert.equal(s.seen.reloads, 1);
  },
  async aRestoreThatGotNoAnswerFromAnEngineThatDoesNotAnswerReloadsWhenAnotherPageIsChosen(w, strict) {
    const s = await settingsWorld(w, strict);
    s.seen.health = 0;
    await s.restore(); await s.ends((r) => r.reject(new TypeError('Failed to fetch')));
    await s.cancel(); await s.page('General');
    assert.equal(s.seen.reloads, 1);
  },
  // Rider R2, the second request: asked again while the first is still under way, the engine refuses it by name
  // (409 restoring). That refusal says nothing of the first: leaving still reloads.
  async aSecondRestoreRefusedWhileTheFirstGotNoAnswerStillReloadsWhenLeft(w, strict) {
    const s = await settingsWorld(w, strict);
    s.seen.health = 0;
    await s.restore(); await s.ends((r) => r.reject(new TypeError('Failed to fetch')));
    await s.again(); await s.ends((r) => r.resolve(response(409, { code: 'restoring' })));
    assert.equal(s.seen.reloads, 0);
    await s.cancel(); await s.close();
    assert.equal(s.seen.reloads, 1);
  },
  // Rider R2: no answer of either kind, and its screen had gone already.
  async aRestoreThatGotNoAnswerOfAnyKindAfterItsScreenWentReloadsTheWindow(w, strict) {
    const s = await settingsWorld(w, strict);
    s.seen.health = 0;
    await s.restore(); await s.replaced();
    await s.ends((r) => r.reject(new TypeError('Failed to fetch')));
    assert.equal(s.seen.reloads, 1);
  },
  // Guard: an answer of no kind, the engine answers: one reload, not two when its screen then goes.
  async aRestoreThatGotNoAnswerReloadsOnceIfTheEngineAnswers(w, strict) {
    const s = await settingsWorld(w, strict);
    await s.restore(); await s.ends((r) => r.reject(new TypeError('Failed to fetch')));
    assert.equal(s.seen.reloads, 1);
  },
  // Guards: a refusal the engine stated changed nothing; a result is shown and nothing reloads before it is left.
  async aRefusalTheEngineStatedReloadsNothing(w, strict) {
    const s = await settingsWorld(w, strict);
    await s.restore(); await s.ends((r) => r.resolve(response(400, { code: 'wrong_passphrase' })));
    await s.cancel(); await s.page('General'); await s.close();
    assert.equal(s.seen.reloads, 0);
    assert.equal(s.seen.healthAsked, 0);
  },
  async aResultIsShownAndNothingReloadsBeforeItIsLeft(w, strict) {
    const s = await settingsWorld(w, strict);
    await s.restore(); await s.ends((r) => r.resolve(response(200, DONE)));
    assert.ok(s.says('Restored.'));
    assert.equal(s.seen.reloads, 0);
    assert.equal(s.seen.healthAsked, 0);
  },
  // r6-1's finding 2, at the window: loaded while the engine says a restore is under way (rule E), the window reads
  // nothing of the database and says so; Try again then reloads it (rider R1).
  async aWindowLoadedWhileARestoreIsUnderWayReadsNothingOfTheDatabase(w, strict) {
    const s = await appWorld(w, strict, { ok: true, restoring: true });
    assert.deepEqual(s.reads(), [], 'nothing but health was asked');
    assert.ok(s.says('Scholia is restoring a backup'));
  },
  async tryAgainOnTheRestoringScreenReloadsTheWindow(w, strict) {
    const s = await appWorld(w, strict, { ok: true, restoring: true });
    await s.press('Try again');
    assert.equal(s.seen.reloads, 1);
    assert.deepEqual(s.reads(), []);
  },
  // Guard: with no restore under way the window reads its launch reads and reloads nothing.
  async aWindowLoadedWithNoRestoreUnderWayReadsItsLaunchReads(w, strict) {
    const s = await appWorld(w, strict, { ok: true });
    assert.ok(s.reads().includes('GET /api/setup') && s.reads().includes('GET /api/settings'));
    assert.equal(s.seen.reloads, 0);
    assert.ok(!s.says('Scholia is restoring a backup'));
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
