// The window's own late feedback and the Sidebar's deletions (the last M2 follow-up PR): the real Shell, ConversationView
// and Sidebar (tests/harness/load.mjs, through Vite) on the stand-in for React, answers deferred and released in a fixed
// order, no timers, plain and with StrictMode's second effect run. As in tests/library_scenarios.test.mjs, each asserts
// on what the drawn tree says or on what was asked of the backend, never on source text.
//   - drop and attach: design review O's Option 1 for addTo's two other callers: an earlier Add that answers late says
//     nothing over a newer one (PR 61 batch 5's P3-2);
//   - delete: design review F's site 3: a Sidebar deletion's change is fired when its request settles, not at Close.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());
const SKIPPED = /notes\.txt/;

// The browser's globals the components touch, the backend's answers deferred until released (GETs answered at once).
function backend(answers = {}) {
  const asked = [];
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    if (method === 'GET') {
      const found = Object.entries(answers).find(([prefix]) => path.startsWith(prefix));
      return found ? Promise.resolve(response(200, found[1](path))) : new Promise(() => {});
    }
    return new Promise((resolve) => asked.push({ method, path,
      answer: (body, status = 200) => resolve(response(status, body)), fail: (code = 'internal', status = 500) => resolve(response(status, { code })) }));
  };
  globalThis.FileReader = class { readAsDataURL(file) { this.result = `data:;base64,${file.data ?? ''}`; queueMicrotask(() => this.onload()); } };
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = () => 0;
  globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400 };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  return asked;
}
const file = (name) => ({ name, size: 10, data: 'eA==' });
const uploads = (r) => r.method === 'POST' && /\/materials$/.test(r.path);
const said = (ui, role) => ui.said((props) => props.role === role);
const windowDrop = (ui) => ui.find((p) => p.onDrop)[0].props.onDrop; // the window's, outside the Library's own
const host = (ui, test, what) => { const found = ui.find(test); if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0].props; };
async function act(ui, fn) { fn(); await ui.settle(); }

const scenarios = {
  // The window's drop: an upload, then a drop of a file Scholia does not read, then the upload fails.
  async drop({ mount, jsx }, strict) {
    const asked = backend({ '/api/projects': () => ({ projects: [{ id: 'p1', name: 'P', kind: 'research' }] }),
      '/api/conversations?': () => ({ conversations: [] }) });
    const { Shell } = await load.component('Shell');
    const ui = await mount([jsx(Shell, { health: {}, settings: { values: {} }, onLanguage() {} })], { strict }).settle();
    const drop = (...files) => act(ui, () => windowDrop(ui)({ preventDefault() {},
      dataTransfer: { types: ['Files'], files } }));
    await drop(file('a.pdf'));
    await drop(file('notes.txt'));
    assert.equal(said(ui, 'alert').filter((s) => SKIPPED.test(s)).length, 1);
    await act(ui, () => asked.filter(uploads)[0].fail('unreachable', 503));
    assert.deepEqual(said(ui, 'alert').map((s) => SKIPPED.test(s)), [true]); // the newer drop's notice stays
  },
  // A drop alone still says how it ended.
  async dropAlone({ mount, jsx }, strict) {
    const asked = backend({ '/api/projects': () => ({ projects: [{ id: 'p1', name: 'P', kind: 'research' }] }),
      '/api/conversations?': () => ({ conversations: [] }) });
    const { Shell } = await load.component('Shell');
    const ui = await mount([jsx(Shell, { health: {}, settings: { values: {} }, onLanguage() {} })], { strict }).settle();
    await act(ui, () => windowDrop(ui)({ preventDefault() {},
      dataTransfer: { types: ['Files'], files: [file('a.pdf')] } }));
    await act(ui, () => asked.filter(uploads)[0].fail('unreachable', 503));
    assert.equal(said(ui, 'alert').filter((s) => /engine is not responding/.test(s)).length, 1);
  },
  // The message box's attach: an upload, then a file Scholia does not read, then the upload answers or fails.
  async attach({ mount, jsx }, strict) {
    for (const ending of ['fail', 'answer']) {
      const asked = backend({ '/api/conversations/c1': () => ({ turns: [], queued: [] }), '/api/asks': () => ({ asks: [] }) });
      const { ConversationView } = await load.component('ConversationView');
      const ui = await mount([jsx(ConversationView, { conversationId: 'c1', conversation: { id: 'c1', title: 'T' }, projectId: 'p1', panel: 'none',
        onPanel() {}, onLibrary() {} }, 'c1')], { strict }).settle();
      const choose = (...files) => act(ui, () => host(ui, (p) => p['data-testid'] === 'composer-files', 'attach')
        .onChange({ target: { files, value: '' } }));
      await choose(file('a.pdf'));
      await choose(file('notes.txt'));
      assert.equal(said(ui, 'status').filter((s) => SKIPPED.test(s)).length, 1);
      const [upload] = asked.filter(uploads);
      await act(ui, () => (ending === 'fail' ? upload.fail('unreachable', 503)
        : upload.answer({ materials: [{ id: 'm1' }], lookup_run_id: null })));
      assert.deepEqual(said(ui, 'status').filter((s) => /notes\.txt|added to the library|engine/.test(s)).map((s) => SKIPPED.test(s)),
        [true], ending);
    }
  },
  // An attach alone still says how it ended.
  async attachAlone({ mount, jsx }, strict) {
    const asked = backend({ '/api/conversations/c1': () => ({ turns: [], queued: [] }), '/api/asks': () => ({ asks: [] }) });
    const { ConversationView } = await load.component('ConversationView');
    const ui = await mount([jsx(ConversationView, { conversationId: 'c1', conversation: { id: 'c1', title: 'T' }, projectId: 'p1', panel: 'none',
      onPanel() {}, onLibrary() {} }, 'c1')], { strict }).settle();
    await act(ui, () => host(ui, (p) => p['data-testid'] === 'composer-files', 'attach')
      .onChange({ target: { files: [file('a.pdf')], value: '' } }));
    await act(ui, () => asked.filter(uploads)[0].answer({ materials: [{ id: 'm1' }], lookup_run_id: null }));
    assert.equal(said(ui, 'status').filter((s) => /added to the library/.test(s)).length, 1);
  },
  // A Sidebar deletion answered with a notice: the dialog says so and stays open, and the change is fired already.
  async deleteConversation({ mount, jsx, library }, strict) {
    const asked = backend();
    const { Sidebar } = await load.component('Sidebar');
    const told = [];
    const before = library.libraryGeneration();
    const ui = await mount([jsx(Sidebar, { projects: [{ id: 'p1', name: 'P', kind: 'research' }], projectId: 'p1',
      conversations: [{ id: 'c1', title: 'T' }], conversationId: 'c1', onProject() {}, onProjectDeleted: () => told.push('projects'),
      onConversation() {}, onConversationDeleted: (removed, known) => told.push(`conversations ${removed} ${known}`), onNewConversation() {},
      onSettings() {} })], { strict }).settle();
    const items = ui.find((p, type) => type === 'ui:DropdownMenuItem' && p.onSelect);
    await act(ui, () => items.find((item) => ui.said((p) => p === item.props)[0] === 'Delete').props.onSelect());
    const remove = ui.find((p, type) => type === 'ui:Button' && p.variant === 'destructive');
    await act(ui, () => remove.at(-1).props.onClick());
    const [deletion] = asked.filter((r) => r.method === 'DELETE');
    assert.match(deletion.path, /\/api\/conversations\/c1/);
    await act(ui, () => deletion.answer({ ok: true, files_left: true }));
    assert.equal(said(ui, 'alert').length, 1); // the dialog says what is left, and stays open
    assert.deepEqual(told, ['conversations c1 true']);
    assert.ok(library.libraryGeneration() > before);
  },
  // A project's deletion that failed: it may have been deleted all the same, so the lists are read again.
  async deleteProjectFailed({ mount, jsx, library }, strict) {
    const asked = backend();
    const { Sidebar } = await load.component('Sidebar');
    const told = [];
    const before = library.libraryGeneration();
    const ui = await mount([jsx(Sidebar, { projects: [{ id: 'p1', name: 'P', kind: 'research' }], projectId: 'p1',
      conversations: [], conversationId: null, onProject() {}, onProjectDeleted: (id, known) => told.push(`projects ${known}`),
      onConversation() {}, onConversationRemoved: () => told.push('conversations'), onNewConversation() {},
      onSettings() {} })], { strict }).settle();
    const items = ui.find((p, type) => type === 'ui:DropdownMenuItem' && p.onSelect);
    await act(ui, () => items.find((item) => /^Delete /.test(ui.said((p) => p === item.props)[0])).props.onSelect());
    await act(ui, () => ui.find((p, type) => type === 'ui:Button' && p.variant === 'destructive').at(-1).props.onClick());
    await act(ui, () => asked.find((r) => r.method === 'DELETE').fail('internal', 500));
    assert.deepEqual(told, ['projects false']);
    assert.ok(library.libraryGeneration() > before);
  },
};

for (const [name, run] of Object.entries(scenarios)) {
  for (const strict of [false, true]) {
    test(`${name}${strict ? ' (StrictMode)' : ''}`, async () => {
      const names = ['fetch', 'FileReader', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
        'requestAnimationFrame', 'window', 'document', 'localStorage'];
      const kept = Object.fromEntries(names.map((n) => [n, Object.getOwnPropertyDescriptor(globalThis, n)]));
      try {
        await run(await load.fresh(), strict);
      } finally {
        for (const [n, d] of Object.entries(kept)) { if (d) Object.defineProperty(globalThis, n, d); else delete globalThis[n]; }
      }
    });
  }
}
