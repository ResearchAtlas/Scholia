// Design review S's scenarios (the window's lists and what is open after a removal's refresh): the real Shell, with its
// Sidebar and ConversationView, on the stand-in (which must honour keys: harness/standin.mjs), every answer released by
// the scenario, every pause ended by it (pause()), no timer running by itself. Each asserts on what the drawn tree says.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const clone = (value) => structuredClone(value);
function backend(state) {
  const asked = []; const held = new Set();
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    const kind = method !== 'GET' ? 'write' : path === '/api/projects' ? 'projects'
      : path.startsWith('/api/conversations?') ? 'list' : /^\/api\/conversations\/[^/?]+$/.test(path) ? 'conversation'
        : path.startsWith('/api/asks') ? 'asks' : 'other';
    const now = () => ({ projects: { projects: clone(state.projects) },
      list: { conversations: clone(state.conversations[new URLSearchParams(path.split('?')[1]).get('project_id')] ?? []) },
      conversation: { turns: [], queued: [] }, asks: { asks: [] } }[kind] ?? { ok: true });
    if (kind === 'other') return new Promise(() => {});
    if (method === 'GET' && !held.has(kind)) return Promise.resolve(response(200, now()));
    return new Promise((resolve) => asked.push({ method, path, kind,
      answer: (body = now(), status = 200) => resolve(response(status, body)),
      fail: (code = 'internal', status = 500) => resolve(response(status, { code })) }));
  };
  globalThis.FileReader = class {};
  const timers = new Map(); let timer = 0; // no timer runs by itself: pause() lets every pause asked for so far end
  globalThis.setTimeout = (fn) => { timers.set(++timer, fn); return timer; }; globalThis.clearTimeout = (id) => { timers.delete(id); };
  globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = () => 0;
  globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400 };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  const store = new Map();
  globalThis.localStorage = state.noStorage ? { getItem() { throw new Error('refused'); }, setItem() { throw new Error('refused'); } }
    : { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  return { asked, pause: () => { const due = [...timers.values()]; timers.clear(); due.forEach((fn) => fn()); }, hold: (kind) => held.add(kind), free: (kind) => held.delete(kind),
    next: (test) => { const i = asked.findIndex(test); if (i < 0) throw new Error('nothing asked'); return asked.splice(i, 1)[0]; } };
}

async function world({ mount, jsx }, strict, state, before = () => {}) {
  const server = backend(state);
  before(server);
  const { Shell } = await load.component('Shell');
  const ui = await mount([jsx(Shell, { health: {}, settings: { values: {} }, onLanguage() {} })], { strict }).settle();
  const off = (x) => x.type === 'drawer:Root' && !x.props.open; // the drawer's copy of the sidebar, closed
  // each element with its ancestors and the text inside it; a closed dialog or menu is not on screen
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) {
      if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
    }
    return out.map((e) => ({ ...e, text: e.text.join(' ') })).filter((e) => ![e, ...e.up].some(off)); };
  const hidden = (e) => [e, ...e.up].some((x) => (x.type === 'ui:Dialog' && !x.props.open) || x.type === 'ui:DropdownMenuContent');
  const act = async (fn) => { fn(); await ui.settle(); };
  const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
  const api = {
    ui, server, elements,
    // what is on screen: everything outside closed dialogs and menus
    screen: () => { const out = []; const stack = [];
      for (const h of ui.hosts()) {
        if (h.props) stack.push(h); else if (h.end) stack.pop();
        else if (!stack.some((x) => off(x) || (x.type === 'ui:Dialog' && !x.props.open) || x.type === 'ui:DropdownMenuContent')) out.push(h.text);
      }
      return out.join(' '); },
    selected: () => elements().filter((e) => e.type === 'button' && e.props['aria-current'] === 'page').map((e) => e.text.trim()),
    draft: () => one(elements().filter((e) => e.type === 'textarea'), 'message box').props.value,
    box: () => elements().filter((e) => e.type === 'textarea').length,
    type: (value) => act(() => one(elements().filter((e) => e.type === 'textarea'), 'message box').props.onChange({ target: { value } })),
    open: (title) => act(() => one(elements().filter((e) => e.type === 'button' && e.props.onClick && e.text.trim() === title && e.up.some((x) => x.type === 'ul')), title).props.onClick()),
    menu: (title, item) => act(() => one(elements().filter((e) => e.type === 'ui:DropdownMenuItem' && e.text.trim() === item && e.up.some((x) => x.type === 'li' && x.text.join(' ').includes(title))), `${title}: ${item}`).props.onSelect()),
    projectMenu: (item) => act(() => one(elements().filter((e) => e.type === 'ui:DropdownMenuItem' && item.test(e.text.trim())), String(item)).props.onSelect()),
    confirmDelete: () => act(() => one(elements().filter((e) => e.type === 'ui:Button' && e.props.variant === 'destructive' && !hidden(e)), 'Delete').props.onClick()),
    submit: (label) => act(() => one(elements().filter((e) => e.type === 'form' && !hidden(e) && e.up.some((x) => x.type === 'ui:Dialog')), label).props.onSubmit({ preventDefault() {} })),
    release: (request, how = (r) => r.answer()) => act(() => how(request)),
    releaseAll: async (test, how = (r) => r.answer()) => { while (server.asked.some(test)) await api.release(server.next(test), how); },
    act,
  };
  return api;
}

const P1 = { id: 'p1', name: 'Project Alpha', kind: 'research' };
const P2 = { id: 'p2', name: 'Project Beta', kind: 'research' };
const conv = (id, title) => ({ id, title, running: false });
const start = () => ({ projects: [P1, P2], conversations: { p1: [conv('a', 'Title Alpha'), conv('b', 'Title Bravo'), conv('c', 'Title Charlie')], p2: [conv('z', 'Title Zulu')] } });
const writes = (method) => (r) => r.method === method;
const lists = (r) => r.kind === 'list';
const DRAFT = 'UNSENT-CANARY-DRAFT';

const scenarios = {
  // Finding 1 (pass 1): a conversation deleted, the list's read after it fails: its title is not on screen.
  async titleGoneThoughReadFails(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Bravo', 'Delete'); await s.confirmDelete();
    s.server.hold('list');
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'b');
    await s.release(s.server.next(writes('DELETE')));
    assert.ok(!s.screen().includes('Title Bravo'), 'as the deletion settles'); // nothing old while the read is unanswered
    await s.releaseAll(lists, (r) => r.fail());
    assert.ok(!s.screen().includes('Title Bravo'), 'after the read failed');
    s.server.free('list'); await s.act(() => s.server.pause()); // it is read again after a pause
    assert.ok(s.screen().includes('Title Charlie') && !s.screen().includes('Title Bravo'), 'after it was read again');
  },
  // Finding 1, the project: a project deleted, the projects' read after it fails: neither its name nor its
  // conversations' titles are on screen.
  async projectGoneThoughReadFails(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    s.server.hold('projects'); s.server.hold('list');
    state.projects = [P2]; delete state.conversations.p1;
    await s.release(s.server.next(writes('DELETE')));
    assert.ok(!/Project Alpha|Title (Alpha|Bravo|Charlie)/.test(s.screen()), `as the deletion settles: ${s.screen()}`);
    await s.releaseAll((r) => r.kind === 'projects', (r) => r.fail());
    assert.ok(!/Project Alpha|Title (Alpha|Bravo|Charlie)/.test(s.screen()), `after the read failed: ${s.screen()}`);
  },
  // This review: a read of the list asked before a deletion and answered after the read asked at its settling puts
  // nothing back.
  async olderReadPutsNothingBack(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    s.server.hold('list');
    await s.menu('Title Charlie', 'Rename'); await s.submit('rename');
    await s.release(s.server.next(writes('PUT'))); // its read of the list is asked, and held
    const older = s.server.next(lists);
    await s.menu('Title Bravo', 'Delete'); await s.confirmDelete();
    await s.release(s.server.next(writes('DELETE')));
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'b');
    await s.release(s.server.next(lists));
    assert.ok(!s.screen().includes('Title Bravo'));
    await s.release(older, (r) => r.answer({ conversations: [conv('a', 'Title Alpha'), conv('b', 'Title Bravo'), conv('c', 'Title Charlie')] }));
    assert.ok(!s.screen().includes('Title Bravo'), 'after the older read answered');
  },
  // Finding 2 (pass 1): A is deleted, its read of the list is late; B is opened and a message typed; the list
  // answers, beginning with C: B stays open, with what was typed.
  async draftKeptWhenTheReadIsLate(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    assert.deepEqual(s.selected(), ['Title Alpha']);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    s.server.hold('list');
    state.conversations.p1 = [conv('c', 'Title Charlie'), conv('b', 'Title Bravo')];
    await s.release(s.server.next(writes('DELETE')));
    if (!s.elements().some((e) => e.type === 'button' && e.text.trim() === 'Title Bravo')) { // a list hidden until it is
      s.server.free('list'); await s.releaseAll(lists); s.server.hold('list'); // read: B is opened once it is shown
    }
    await s.open('Title Bravo'); await s.type(DRAFT);
    s.server.free('list'); await s.releaseAll(lists);
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.equal(s.draft(), DRAFT);
  },
  // What is open when the answer comes is what is compared, not what was open when Delete was pressed: B is opened and
  // typed in while A's deletion is still asked (the stand-in calls handlers behind the dialog; a browser's dialog is modal).
  async whatIsOpenNowIsCompared(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    await s.open('Title Bravo'); await s.type(DRAFT);
    state.conversations.p1 = [conv('c', 'Title Charlie'), conv('b', 'Title Bravo')];
    await s.release(s.server.next(writes('DELETE')));
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.equal(s.draft(), DRAFT);
  },
  // The same order with a new conversation started meanwhile, which needs no list: it stays, with what was typed.
  async draftKeptInANewConversationWhenTheReadIsLate(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    s.server.hold('list');
    state.conversations.p1 = [conv('c', 'Title Charlie'), conv('b', 'Title Bravo')];
    await s.release(s.server.next(writes('DELETE')));
    await s.act(() => s.elements().filter((e) => e.type === 'ui:Button' && e.text.trim() === 'New conversation')[0].props.onClick());
    await s.type(DRAFT);
    s.server.free('list'); await s.releaseAll(lists);
    assert.deepEqual(s.selected(), []);
    assert.equal(s.draft(), DRAFT);
  },
  // Guards (green at the head too): a refresh that fails with nothing removed keeps the list; a deleted conversation
  // that was open is left for the project's first.
  async aRefreshThatFailsKeepsTheList(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    s.server.hold('list');
    await s.menu('Title Charlie', 'Rename'); await s.submit('rename');
    await s.release(s.server.next(writes('PUT')));
    await s.releaseAll(lists, (r) => r.fail());
    assert.ok(['Title Alpha', 'Title Bravo', 'Title Charlie'].every((title) => s.screen().includes(title)));
    assert.deepEqual(s.selected(), ['Title Alpha']);
  },
  async aDeletedOpenConversationIsLeftForTheFirst(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a');
    await s.release(s.server.next(writes('DELETE')));
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.ok(!s.screen().includes('Title Alpha'));
  },
  // Finding 2 (pass 2): a message typed in A; A's deletion fails and the list's read after it fails: A stays
  // open, with what was typed.
  async draftKeptWhenDeletionAndReadFail(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    s.server.hold('list');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail());
    await s.releaseAll(lists, (r) => r.fail());
    assert.equal(s.draft(), DRAFT);
    s.server.free('list'); await s.releaseAll(lists);
    assert.equal(s.draft(), DRAFT, 'after a later read');
  },
  // The same for a project: a message typed; the project's deletion fails; whatever the reads then do, the
  // conversation stays open with what was typed.
  async draftKeptWhenProjectDeletionFails(w, strict) {
    for (const noStorage of [false, true]) {
      const state = { ...start(), noStorage }; state.projects = [P2, P1]; // p2 comes first; the researcher opens p1
      const s = await world(await load.fresh(), strict, state); // a module graph each: nothing of the first half in the second
      await s.projectMenu(/^Project Alpha$/);
      assert.deepEqual(s.selected(), ['Title Alpha']);
      await s.type(DRAFT);
      await s.projectMenu(/^Delete /); await s.confirmDelete();
      await s.release(s.server.next(writes('DELETE')), (r) => r.fail());
      assert.deepEqual(s.selected(), ['Title Alpha'], `storage ${noStorage ? 'refused' : 'kept'}`);
      assert.equal(s.draft(), DRAFT, `storage ${noStorage ? 'refused' : 'kept'}`);
    }
  },
  // This review: typed before the window's first reads answer: nothing typed is lost when they do.
  async nothingTypedBeforeTheListsAnswerIsLost(w, strict) {
    for (const kind of ['projects', 'list']) {
      const state = start(); const s = await world(w, strict, state, (server) => server.hold(kind));
      if (s.box() === 0) { // no message box until the window knows what it shows: nothing can be typed, so nothing lost
        s.server.free(kind); await s.releaseAll((r) => r.kind === kind);
        assert.equal(s.box(), 1, kind);
        continue;
      }
      await s.type(DRAFT);
      s.server.free(kind); await s.releaseAll((r) => r.kind === kind);
      assert.equal(s.draft(), DRAFT, kind);
    }
  },
  // This review: a conversation moved to another project is left at once, in the researcher's own action, or not at
  // all: never at a later read, with what was typed since.
  async aMovedConversationIsNotLeftAtALaterRead(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Move');
    await s.act(() => s.elements().filter((e) => e.type === 'input' && e.props.type === 'radio' && e.props.value === 'p2')[0].props.onChange());
    await s.submit('move');
    s.server.hold('list');
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a');
    await s.release(s.server.next(writes('POST')));
    const typed = s.box() > 0; // no message box while what is shown is not decided: nothing can be typed
    if (typed) await s.type(DRAFT);
    s.server.free('list'); await s.releaseAll(lists);
    assert.equal(s.box(), 1);
    if (typed) assert.equal(s.draft(), DRAFT);
    assert.ok(!s.selected().includes('Title Alpha'));
  },
  // A deletion that happened though its request failed (its answer lost): the title is gone once the list is read, and
  // what was typed is still in its box.
  async aLostAnswerLosesNothingTyped(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    assert.ok(!s.screen().includes('Title Alpha'));
    assert.equal(s.box() && s.draft(), DRAFT);
  },
  // The weaker property (the brief's second example): what was typed is there again on returning to its conversation.
  async typedIsThereOnReturning(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    s.server.hold('list');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail());
    await s.releaseAll(lists, (r) => r.fail());
    s.server.free('list'); await s.act(() => s.server.pause()); await s.releaseAll(lists);
    if (!s.selected().includes('Title Alpha')) await s.open('Title Alpha');
    assert.equal(s.draft(), DRAFT);
  },
};

const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
for (const [name, run] of Object.entries(scenarios)) {
  if (only && !only.includes(name)) continue;
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
