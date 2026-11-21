// Design review U's scenarios (late answers changing a newer selection, panel or read in the window): the real Shell, with
// its Sidebar and ConversationView, on the stand-in (harness/standin.mjs), every answer released by the scenario, every
// pause ended by it, no timer running by itself, no sleeps. Each asserts on what the drawn tree says. The helpers are
// window_lists.test.mjs's, with a stream, a file reader, a window that can be resized and a dialog that can be cancelled.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const clone = (value) => structuredClone(value);
function backend(state) {
  // With design review Z's: the backend answers as backend/app.py does once a record is deleted (state.gone): 404
  // not_found for the conversation's record, its spend and its project's settings; 200 and an empty list for a deleted
  // project's conversations. failing(kind): reads of it fail so. A spend read with no state.spend is never answered.
  const asked = []; const held = new Set(); const failing = new Map();
  state.gone ??= new Set();
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const query = (path, name = 'project_id') => new URLSearchParams(path.split('?')[1]).get(name);
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    const kind = method !== 'GET' ? 'write' : path === '/api/projects' ? 'projects'
      : path.startsWith('/api/conversations?') ? 'list' : /^\/api\/conversations\/[^/?]+$/.test(path) ? 'conversation'
        : path.startsWith('/api/asks') ? 'asks' : path.startsWith('/api/settings?') ? 'settings'
          : path === '/api/settings' ? 'choice' : /\/spend\?/.test(path) ? 'spend' : /^\/api\/projects\/[^/?]+$/.test(path) ? 'project' : 'other';
    const id = kind === 'conversation' ? path.split('/').pop() : kind === 'spend' || kind === 'asks' ? query(path, 'conversation_id') : null;
    const project = kind === 'settings' ? query(path) : kind === 'spend' ? path.split('/')[3] : kind === 'project' ? path.split('/').pop() : null;
    const missing = () => (id && state.gone.has(id)) || (project && state.gone.has(project));
    const now = () => ({ projects: { projects: clone(state.projects) },
      list: { conversations: clone(state.conversations[query(path)] ?? []) },
      conversation: clone(state.records?.[id] ?? { turns: [], queued: [] }),
      asks: { asks: missing() ? [] : clone(state.asks?.[id] ?? []), working: 0 },
      choice: { values: clone(state.personal ?? {}), hash: 'h' },
      settings: { hash: 'h', values: { ui: { panel: state.panels?.[query(path)] ?? 'none' } } },
      spend: clone(state.spend?.[id] ?? {}),
      project: clone(state.projects.find((p) => p.id === project) ?? {}),
    }[kind] ?? { ok: true });
    const answer = () => (failing.has(kind) ? response(failing.get(kind)[1], { code: failing.get(kind)[0] })
      : kind !== 'asks' && missing() ? response(404, { code: 'not_found' }) : response(200, now()));
    if (kind === 'other' || (kind === 'spend' && !state.spend)) return new Promise(() => {});
    if (method === 'GET' && !held.has(kind)) return Promise.resolve(answer());
    return new Promise((resolve, reject) => { init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      asked.push({ method, path, kind, signal: init.signal, body: init.body ? JSON.parse(init.body) : null,
        answer: (body, status = 200) => resolve(body === undefined && method === 'GET' ? answer() : response(status, body ?? now())),
        // status 0: no answer at all (fetch rejects, as when the backend cannot be reached)
        fail: (code = 'internal', status = 500) => (status === 0 ? reject(new TypeError('Failed to fetch')) : resolve(response(status, { code }))),
        // a turn's stream: these events, and it stays open (the turn runs on)
        stream: (...events) => resolve({ ok: true, status: 200, body: new ReadableStream({ start(controller) {
          for (const event of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)); } }) }),
        // a turn's stream, opened: push(...events) sends them, end() closes it
        open() { let controller; resolve({ ok: true, status: 200, body: new ReadableStream({ start(c) { controller = c; } }) });
          return { push: (...events) => { for (const event of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)); },
            end: () => controller.close() }; } }); });
  };
  globalThis.FileReader = class { readAsDataURL(file) { this.result = `data:;base64,${file.data ?? ''}`; queueMicrotask(() => this.onload()); } };
  const timers = new Map(); let timer = 0; // no timer runs by itself: pause() lets every pause asked for so far end
  globalThis.setTimeout = (fn) => { timers.set(++timer, fn); return timer; }; globalThis.clearTimeout = (id) => { timers.delete(id); };
  globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = () => 0;
  const listeners = new Set();
  globalThis.window = { addEventListener(type, fn) { if (type === 'resize') listeners.add(fn); }, removeEventListener(type, fn) { listeners.delete(fn); },
    innerWidth: state.width ?? 1400 };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  return { asked, pause: () => { const due = [...timers.values()]; timers.clear(); due.forEach((fn) => fn()); }, hold: (kind) => held.add(kind), free: (kind) => held.delete(kind),
    failing: (kind, code = 'internal', status = 500) => failing.set(kind, [code, status]), working: (kind) => failing.delete(kind),
    resize: (width) => { globalThis.window.innerWidth = width; [...listeners].forEach((fn) => fn()); },
    next: (test) => { const i = asked.findIndex(test); if (i < 0) throw new Error(`nothing asked: ${asked.map((r) => `${r.method} ${r.path}`).join(', ')}`); return asked.splice(i, 1)[0]; } };
}

async function world({ mount, jsx }, strict, state, before = () => {}) {
  const server = backend(state);
  before(server);
  const { Shell } = await load.component('Shell');
  const ui = await mount([jsx(Shell, { health: {}, settings: { values: {} }, onLanguage() {} })], { strict }).settle();
  const off = (x) => x.type === 'drawer:Root' && !x.props.open; // the drawer's copy of the sidebar, closed
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) {
      if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
    }
    return out.map((e) => ({ ...e, text: e.text.join(' ') })).filter((e) => ![e, ...e.up].some(off)); };
  const hidden = (e) => [e, ...e.up].some((x) => (x.type === 'ui:Dialog' && !x.props.open) || x.type === 'ui:DropdownMenuContent');
  const act = async (fn) => { fn(); await ui.settle(); };
  const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
  const dialogs = () => elements().filter((e) => e.type === 'ui:Dialog' && e.props.open);
  const api = {
    ui, server, elements, dialogs,
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
    send: () => act(() => { one(elements().filter((e) => e.type === 'form' && !e.up.some((x) => x.type === 'ui:Dialog')), 'message form').props.onSubmit({ preventDefault() {} }); }),
    attach: (...files) => act(() => one(elements().filter((e) => e.props['data-testid'] === 'composer-files'), 'files').props.onChange({ target: { files, value: '' } })),
    pressed: (label) => elements().filter((e) => e.type === 'ui:Button' && e.props['aria-pressed'] === true && e.text.includes(label)).length === 1,
    panel: (label) => act(() => one(elements().filter((e) => e.type === 'ui:Button' && 'aria-pressed' in e.props && e.text.includes(label)), label).props.onClick({ currentTarget: null })),
    open: (title) => act(() => one(elements().filter((e) => e.type === 'button' && e.props.onClick && e.text.trim() === title && e.up.some((x) => x.type === 'ul')), title).props.onClick()),
    menu: (title, item) => act(() => one(elements().filter((e) => e.type === 'ui:DropdownMenuItem' && e.text.trim() === item && e.up.some((x) => x.type === 'li' && x.text.join(' ').includes(title))), `${title}: ${item}`).props.onSelect()),
    projectMenu: (item) => act(() => one(elements().filter((e) => e.type === 'ui:DropdownMenuItem' && item.test(e.text.trim())), String(item)).props.onSelect()),
    newConversation: () => act(() => elements().filter((e) => e.type === 'ui:Button' && e.text.trim() === 'New conversation')[0].props.onClick()),
    field: (id, value) => act(() => one(elements().filter((e) => e.type === 'ui:Input' && e.props.id === id && !hidden(e)), id).props.onChange({ target: { value } })),
    fieldValue: (id) => elements().filter((e) => e.type === 'ui:Input' && e.props.id === id && !hidden(e)).map((e) => e.props.value)[0],
    submit: (label) => act(() => { one(elements().filter((e) => e.type === 'form' && !hidden(e) && e.up.some((x) => x.type === 'ui:Dialog')), label).props.onSubmit({ preventDefault() {} }); }),
    // Cancel, as the researcher can: the button if it is enabled, then Escape (the dialog's own closing). Whether it closed.
    cancel: async () => {
      const [dialog] = dialogs(); if (!dialog) throw new Error('no dialog open');
      const button = elements().filter((e) => e.type === 'ui:Button' && e.text.trim() === 'Cancel' && !hidden(e) && !e.props.disabled)[0];
      await act(() => { if (button) button.props.onClick(); });
      if (dialogs().length) await act(() => dialogs()[0].props.onOpenChange(false));
      return dialogs().length === 0;
    },
    confirmDelete: () => act(() => one(elements().filter((e) => e.type === 'ui:Button' && e.props.variant === 'destructive' && !hidden(e)), 'Delete').props.onClick()),
    release: (request, how = (r) => r.answer()) => act(() => how(request)),
    releaseAll: async (test, how = (r) => r.answer()) => { while (server.asked.some(test)) await api.release(server.next(test), how); },
    act,
    // design review Z's: what is shown (outside closed dialogs, menus and the drawer), with labels, titles and values
    shown: () => elements().filter((e) => !hidden(e)),
    seen: () => { const out = []; const stack = [];
      const covered = (x) => off(x) || (x.type === 'ui:Dialog' && !x.props.open) || x.type === 'ui:DropdownMenuContent';
      for (const h of ui.hosts()) {
        if (h.props) { stack.push(h); if (!stack.some(covered)) out.push(...[h.props['aria-label'], h.props.title, h.props.value].filter((x) => typeof x === 'string')); }
        else if (h.end) stack.pop(); else if (!stack.some(covered)) out.push(h.text);
      }
      return out.join(' '); },
    buttons: (label) => elements().filter((e) => !hidden(e) && (e.type === 'ui:Button' || e.type === 'button') && e.props.onClick && e.text.trim() === label),
    press: (label) => act(() => one(api.buttons(label), label).props.onClick({ preventDefault() {}, currentTarget: null })),
    meters: () => elements().filter((e) => !hidden(e) && e.props.role === 'meter'),
    closeDialog: () => act(() => { const [d] = dialogs(); if (d) d.props.onOpenChange(false); }),
    pause: () => act(() => server.pause()),
  };
  return api;
}


const P1 = { id: 'p1', name: 'Project Alpha', kind: 'research' };
const P2 = { id: 'p2', name: 'Project Beta', kind: 'research' };
const P3 = { id: 'p3', name: 'Project Gamma', kind: 'research' };
const conv = (id, title) => ({ id, title, running: false });
const start = () => ({ projects: [P1, P2], conversations: { p1: [conv('a', 'Title Alpha'), conv('b', 'Title Bravo'), conv('c', 'Title Charlie')], p2: [conv('z', 'Title Zulu')] } });
const writes = (method, part = '') => (r) => r.method === method && r.path.includes(part);
const lists = (r) => r.kind === 'list';
const DRAFT = 'UNSENT-CANARY-DRAFT';
const file = (name) => ({ name, size: 10, data: 'eA==' });
const moveTo = async (s, title, target) => {
  await s.menu(title, 'Move');
  await s.act(() => s.elements().filter((e) => e.type === 'input' && e.props.type === 'radio' && e.props.value === target)[0].props.onChange());
  await s.submit('move');
};

// Design review Z (what the window still holds or shows after a deletion; PR 63's recheck r4, pass 1, findings 1 and 3)
const QUEUED = 'DELETED-QUEUED-CANARY';
const ASKED = 'DELETED-QUESTION-CANARY';
const NOTE = 'DELETED-STEERING-CANARY';
const MEMORY = 'DELETED-MEMORY-CANARY';
const LIVE = 'DELETED-LIVE-CANARY';
const SPEND = { conversation: { limit_usd: 1, settled_usd: 0.9 }, project: { limit_usd: 100, settled_usd: 1 }, notice_percents: [50, 85, 90] };
// A's record, a canary in each field the view draws (the answer's text is drawn by the Markdown part, a stub here).
const recordA = () => ({ queued: [{ id: 'q1', text: QUEUED }], turns: [{ run_id: 'r0', status: 'succeeded', result_saved: true,
  message: { text: ASKED }, answer: { text: 'an answer' }, citations: [], cost_usd: 0,
  steering: [{ seq: 1, text: NOTE, state: 'read' }],
  memory: [{ action: 'saved', count: 1, records: [{ id: 'm1', type: 'fact', status: 'confirmed', content: MEMORY, scope: 'project' }] }] }] });
const withRecord = () => ({ projects: [P1, P2], records: { a: recordA() }, spend: { a: SPEND },
  conversations: { p1: [conv('a', 'Title Alpha'), conv('b', 'Title Bravo'), conv('c', 'Title Charlie')], p2: [conv('z', 'Title Zulu')] } });
const CANARIES = [QUEUED, ASKED, NOTE, MEMORY, LIVE, 'Title Alpha'];
const left = (s) => CANARIES.filter((canary) => s.seen().includes(canary));
// the server deletes it: what it then answers
const deleteConversation = (state, id, project = 'p1') => { state.gone.add(id); state.conversations[project] = state.conversations[project].filter((c) => c.id !== id); };
const deleteProject = (state, id) => { state.gone.add(id); for (const c of state.conversations[id] ?? []) state.gone.add(c.id);
  state.projects = state.projects.filter((p) => p.id !== id); delete state.conversations[id]; };
const goes = async (s) => { await s.act(() => s.server.resize(800)); assert.equal(s.dialogs().length, 0, 'the dialog went with the sidebar'); await s.act(() => s.server.resize(1400)); };


const scenarios = {
  // r2-1 finding 1: A is moved to the other project and the answer is late; A is opened there and a message typed; the
  // answer comes: A stays open there, with what was typed. (Behind the dialog when it cannot be cancelled: the stand-in
  // calls handlers a modal dialog would cover, so the handler itself is what is tested.)
  async aMoveAnsweredLateLeavesWhatWasOpenedSince(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await moveTo(s, 'Title Alpha', 'p2');
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a'); state.conversations.p2 = [conv('z', 'Title Zulu'), conv('a', 'Title Alpha')];
    await s.cancel();
    await s.projectMenu(/^Project Beta$/);
    await s.open('Title Alpha'); await s.type(DRAFT);
    await s.release(s.server.next(writes('POST', '/move')));
    assert.deepEqual(s.selected(), ['Title Alpha']);
    assert.equal(s.box() && s.draft(), DRAFT);
  },
  // The same answer after A was moved back and opened again where it was: a later visit of the same conversation in the
  // same project is not the one the move was asked in.
  async aMoveAnsweredAfterItWasMovedBackLeavesIt(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await moveTo(s, 'Title Alpha', 'p2');
    const first = s.server.next(writes('POST', '/move'));
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a'); state.conversations.p2 = [conv('z', 'Title Zulu'), conv('a', 'Title Alpha')];
    await s.cancel();
    await s.projectMenu(/^Project Beta$/);
    await s.open('Title Alpha');
    await moveTo(s, 'Title Alpha', 'p1');
    state.conversations.p2 = [conv('z', 'Title Zulu')]; state.conversations.p1 = [conv('b', 'Title Bravo'), conv('c', 'Title Charlie'), conv('a', 'Title Alpha')];
    await s.release(s.server.next(writes('POST', '/move')));
    await s.projectMenu(/^Project Alpha$/);
    await s.open('Title Alpha'); await s.type(DRAFT);
    await s.release(first);
    assert.deepEqual(s.selected(), ['Title Alpha']);
    assert.equal(s.box() && s.draft(), DRAFT);
  },
  // r2-1 finding 2: a new project is asked for and the answer is late; another project is opened and a message typed;
  // the answer comes: what is open stays open, with what was typed, and the new project is listed.
  async aNewProjectAnsweredLateOpensNothingOverALaterChoice(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.projectMenu(/^New project$/); await s.field('new-project', 'Project Gamma'); await s.submit('new project');
    await s.cancel();
    await s.projectMenu(/^Project Beta$/);
    await s.type(DRAFT);
    state.projects = [P1, P2, P3];
    await s.release(s.server.next(writes('POST', '/api/projects')), (r) => r.answer(P3));
    assert.deepEqual(s.selected(), ['Title Zulu']);
    assert.equal(s.box() && s.draft(), DRAFT);
    assert.ok(s.elements().some((e) => e.type === 'ui:DropdownMenuItem' && e.text.trim() === 'Project Gamma'), 'the new project is listed');
  },
  // The same with nothing chosen since, only typed: after a cancel the answer opens nothing and closes no other dialog;
  // where a request under way keeps its dialog (it cannot be cancelled), nothing can be typed behind it.
  async aCancelledDialogsAnswerChangesNothingTypedSince(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.projectMenu(/^New project$/); await s.field('new-project', 'Project Gamma'); await s.submit('new project');
    const cancelled = await s.cancel();
    if (!cancelled) { // the dialog stays while its request is under way, as the delete dialog does
      state.projects = [P1, P2, P3]; state.conversations.p3 = [];
      await s.release(s.server.next(writes('POST', '/api/projects')), (r) => r.answer(P3));
      assert.equal(s.dialogs().length, 0, 'it closes when its request ends');
      assert.ok(s.screen().includes('Project Gamma'), 'and the new project opens');
      return;
    }
    await s.type(DRAFT);
    await s.menu('Title Charlie', 'Rename'); await s.field('rename', 'Typed title');
    state.projects = [P1, P2, P3];
    await s.release(s.server.next(writes('POST', '/api/projects')), (r) => r.answer(P3));
    assert.deepEqual(s.selected(), ['Title Alpha']);
    assert.equal(s.box() && s.draft(), DRAFT);
    assert.equal(s.fieldValue('rename'), 'Typed title', 'another dialog opened since stays open');
  },
  // r2-1 finding 3: a rename is asked in one project and its answer is late; another project is opened; the answer
  // comes: the open project's list is shown and its first conversation opened, not "Loading…" for good.
  async aRenameAnsweredLateLeavesTheOpenProjectsListRead(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Charlie', 'Rename'); await s.field('rename', 'Renamed'); await s.submit('rename');
    await s.cancel();
    s.server.hold('list');
    await s.projectMenu(/^Project Beta$/);
    await s.release(s.server.next(writes('PUT')));
    s.server.free('list'); await s.releaseAll(lists);
    assert.deepEqual(s.selected(), ['Title Zulu']);
    assert.equal(s.box(), 1);
    assert.ok(!s.elements().some((e) => e.type === 'li' && e.props.role === 'status'), 'the list is not loading');
  },
  // The other half of the same defect: the list read by an obsolete reload is one project's, kept under another's name;
  // on going back to that other project it is never shown as its own, and none of its conversations is opened there.
  async aListReadForOneProjectIsNeverShownAsAnothers(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Charlie', 'Rename'); await s.field('rename', 'Renamed'); await s.submit('rename');
    await s.cancel();
    s.server.hold('list');
    await s.projectMenu(/^Project Beta$/);
    await s.release(s.server.next(writes('PUT')));
    await s.releaseAll(lists); // every read asked so far answers; reads asked from here on are held
    await s.projectMenu(/^Project Alpha$/);
    assert.ok(!s.screen().includes('Title Zulu'), `Project Beta's list under Project Alpha: ${s.screen()}`);
    assert.ok(!s.selected().includes('Title Zulu'));
    s.server.free('list'); await s.releaseAll(lists);
    assert.ok(!s.selected().includes('Title Zulu'), 'after its own list answered');
    assert.ok(s.screen().includes('Title Alpha') && !s.screen().includes('Title Zulu'));
  },
  // r2-2 finding 3, first site: files are attached in a new conversation and the answer is late; another project is
  // opened and a message typed; the answer comes: that project's panel is as it was.
  async anAttachAnsweredLateOpensNoPanelElsewhere(w, strict) {
    const state = { ...start(), panels: { p1: 'none', p2: 'none' } }; const s = await world(w, strict, state);
    await s.newConversation();
    await s.attach(file('paper.pdf'));
    await s.projectMenu(/^Project Beta$/);
    await s.type(DRAFT);
    assert.ok(!s.pressed('Library'), 'before the answer');
    await s.release(s.server.next(writes('POST', '/materials')), (r) => r.answer({ materials: [{ id: 'm1' }], lookup_run_id: null }));
    assert.ok(!s.pressed('Library'), 'after the answer');
    assert.equal(s.draft(), DRAFT);
  },
  // The same in the conversation it was attached in: the panel chosen since stays.
  async anAttachAnsweredLateKeepsThePanelChosenSince(w, strict) {
    const state = { ...start(), panels: { p1: 'none', p2: 'none' } }; const s = await world(w, strict, state);
    await s.newConversation();
    await s.attach(file('paper.pdf'));
    await s.panel('Manuscript');
    assert.ok(s.pressed('Manuscript'));
    await s.release(s.server.next(writes('POST', '/materials')), (r) => r.answer({ materials: [{ id: 'm1' }], lookup_run_id: null }));
    assert.ok(s.pressed('Manuscript') && !s.pressed('Library'));
  },
  // Guard (green at the head): files attached in a new conversation open the Library, at the latest when they are added.
  async anAttachInANewConversationOpensTheLibrary(w, strict) {
    const state = { ...start(), panels: { p1: 'none', p2: 'none' } }; const s = await world(w, strict, state);
    await s.newConversation();
    await s.attach(file('paper.pdf'));
    await s.release(s.server.next(writes('POST', '/materials')), (r) => r.answer({ materials: [{ id: 'm1' }], lookup_run_id: null }));
    assert.ok(s.pressed('Library'));
  },
  // r2-2 finding 3, second site: the project's settings are read late; a panel is chosen meanwhile; the read answers:
  // the panel chosen stays.
  async aSettingsReadAnsweredLateKeepsThePanelChosen(w, strict) {
    const state = { ...start(), panels: { p1: 'none', p2: 'none' } };
    const s = await world(w, strict, state, (server) => server.hold('settings'));
    await s.panel('Library');
    assert.ok(s.pressed('Library'));
    await s.releaseAll((r) => r.kind === 'settings' && r.method === 'GET', (r) => r.answer({ hash: 'h', values: { ui: { panel: 'none' } } }));
    assert.ok(s.pressed('Library'));
  },
  // A project left and opened again: the settings read asked at the earlier visit decides nothing at the later one.
  async aSettingsReadOfAnEarlierVisitDecidesNoPanel(w, strict) {
    const state = { ...start(), panels: { p1: 'none', p2: 'none' } };
    const s = await world(w, strict, state, (server) => server.hold('settings'));
    const earlier = s.server.asked.filter((r) => r.kind === 'settings' && r.path.includes('p1')).length; // StrictMode asks twice
    const older = Array.from({ length: earlier }, () => s.server.next((r) => r.kind === 'settings' && r.path.includes('p1')));
    await s.projectMenu(/^Project Beta$/);
    await s.projectMenu(/^Project Alpha$/);
    await s.releaseAll((r) => r.kind === 'settings' && r.path.includes('p1')); // the later visit's read: none
    assert.ok(!s.pressed('Library'));
    for (const r of older) await s.release(r, (x) => x.answer({ hash: 'h', values: { ui: { panel: 'library' } } }));
    assert.ok(!s.pressed('Library'), 'after the earlier visit\'s read answered');
  },
  // Guard (green at the head): the same two reads answering oldest first: the later visit's read is the one that decides.
  async aSettingsReadOfAnEarlierVisitAnsweringFirstDecidesNoPanel(w, strict) {
    const state = { ...start(), panels: { p1: 'none', p2: 'none' } };
    const s = await world(w, strict, state, (server) => server.hold('settings'));
    const earlier = s.server.asked.filter((r) => r.kind === 'settings' && r.path.includes('p1')).length;
    const older = Array.from({ length: earlier }, () => s.server.next((r) => r.kind === 'settings' && r.path.includes('p1')));
    await s.projectMenu(/^Project Beta$/);
    await s.projectMenu(/^Project Alpha$/);
    for (const r of older) await s.release(r, (x) => x.answer({ hash: 'h', values: { ui: { panel: 'library' } } }));
    await s.releaseAll((r) => r.kind === 'settings' && r.path.includes('p1'));
    assert.ok(!s.pressed('Library'));
  },
  // Guard (green at the head): another conversation opened, or a new one started, begins with an empty message box and
  // its own turns; what was typed in the one left is not carried over.
  async anotherConversationOpenedStartsEmpty(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.open('Title Bravo');
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.equal(s.draft(), '');
    await s.type(DRAFT);
    await s.newConversation();
    assert.deepEqual(s.selected(), []);
    assert.equal(s.draft(), '');
    await s.type(DRAFT);
    await s.projectMenu(/^Project Beta$/);
    assert.deepEqual(s.selected(), ['Title Zulu']);
    assert.equal(s.draft(), '');
  },
  // Guard (green at the head): a project's remembered panel opens when its settings are read and nothing was chosen.
  async aProjectsRememberedPanelOpens(w, strict) {
    const state = { ...start(), panels: { p1: 'library', p2: 'none' } };
    const s = await world(w, strict, state, (server) => server.hold('settings'));
    assert.ok(!s.pressed('Library'));
    await s.releaseAll((r) => r.kind === 'settings');
    assert.ok(s.pressed('Library'));
  },
  // S's case left ("typed while a new conversation's first message is admitted"): the first message is sent, the next is
  // typed while the first is admitted, and the admission's answer comes: what was typed is still in the box.
  async typedWhileAFirstMessageIsAdmittedIsKept(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.newConversation();
    await s.type('First message'); await s.send();
    await s.release(s.server.next(writes('POST', '/api/conversations')), (r) => r.answer({ id: 'n1' }));
    await s.type(DRAFT);
    state.conversations.p1 = [conv('n1', 'Title New'), ...state.conversations.p1];
    await s.release(s.server.next(writes('POST', '/message/stream')), (r) => r.stream({ type: 'run_started', run_id: 'r1', conversation_id: 'n1', seq: 1 }));
    assert.deepEqual(s.selected(), ['Title New'], 'the window knows the conversation made');
    assert.equal(s.draft(), DRAFT);
  },
  // Guard (green at the head): a new project is opened when it is made, and a moved open conversation is left.
  async aNewProjectIsOpenedWhenItIsMade(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.projectMenu(/^New project$/); await s.field('new-project', 'Project Gamma'); await s.submit('new project');
    state.projects = [P1, P2, P3]; state.conversations.p3 = [];
    await s.release(s.server.next(writes('POST', '/api/projects')), (r) => r.answer(P3));
    assert.equal(s.dialogs().length, 0);
    assert.deepEqual(s.selected(), []);
    assert.equal(s.box(), 1);
    assert.ok(s.elements().some((e) => e.type === 'button' && e.props['aria-label'] === 'Switch project' && e.text.includes('Project Gamma')), s.screen());
  },
  async aMovedOpenConversationIsLeft(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await moveTo(s, 'Title Alpha', 'p2');
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a');
    await s.release(s.server.next(writes('POST', '/move')));
    assert.deepEqual(s.selected(), ['Title Bravo']);
  },
  // Cases this review leaves under the recommended option, kept as scenarios so they are not found again as new: the
  // window is narrowed while a move's (or a new project's) request is under way, which takes the Sidebar and its dialog
  // away; a message is typed; the answer comes.
  // The phase C internal review (F3): each of the three dialogs stays while its request is under way, and says so.
  async aDialogWhoseRequestIsUnderWayStaysAndSaysSo(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    const stays = async (what, request) => {
      assert.equal(await s.cancel(), false, `${what}: it stays`);
      assert.ok(s.screen().includes('Working…'), `${what}: it says so`);
      await s.release(s.server.next(request));
    };
    await s.projectMenu(/^New project$/); await s.field('new-project', 'Project Gamma'); await s.submit('new project');
    state.projects = [P1, P2, P3]; state.conversations.p3 = [];
    await stays('New project', writes('POST', '/api/projects'));
    await s.projectMenu(/^Project Alpha$/);
    await s.menu('Title Charlie', 'Rename'); await s.field('rename', 'Renamed'); await s.submit('rename');
    await stays('Rename', writes('PUT'));
    await moveTo(s, 'Title Bravo', 'p2');
    await stays('Move', writes('POST', '/move'));
  },
  // The phase C internal review (F4): a deletion known to have happened leaves what it removed, though its sidebar went
  // (the window narrowed during the request): the deleted project is not left open (rider W is for what still exists).
  async aDeletedOpenProjectIsLeftThoughTheSidebarWent(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    state.projects = [P2]; delete state.conversations.p1;
    await s.act(() => s.server.resize(800));
    await s.release(s.server.next(writes('DELETE')));
    await s.act(() => s.server.resize(1400));
    assert.deepEqual(s.selected(), ['Title Zulu']);
  },
  // The same for the open conversation (the recheck of the phase C review's fixes).
  async aDeletedOpenConversationIsLeftThoughTheSidebarWent(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a');
    await s.act(() => s.server.resize(800));
    await s.release(s.server.next(writes('DELETE')));
    await s.act(() => s.server.resize(1400));
    assert.deepEqual(s.selected(), ['Title Bravo']);
  },
  async typedAfterAMovesDialogWentWithTheSidebar(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await moveTo(s, 'Title Alpha', 'p2');
    state.conversations.p1 = state.conversations.p1.filter((c) => c.id !== 'a');
    await s.act(() => s.server.resize(800));
    assert.equal(s.dialogs().length, 0, 'the dialog went with the sidebar');
    await s.type(DRAFT);
    await s.release(s.server.next(writes('POST', '/move')));
    assert.equal(s.box() && s.draft(), DRAFT);
  },
  async typedAfterANewProjectsDialogWentWithTheSidebar(w, strict) {
    const state = start(); const s = await world(w, strict, state);
    await s.projectMenu(/^New project$/); await s.field('new-project', 'Project Gamma'); await s.submit('new project');
    await s.act(() => s.server.resize(800));
    assert.equal(s.dialogs().length, 0, 'the dialog went with the sidebar');
    await s.type(DRAFT);
    state.projects = [P1, P2, P3]; state.conversations.p3 = [];
    await s.release(s.server.next(writes('POST', '/api/projects')), (r) => r.answer(P3));
    assert.equal(s.box() && s.draft(), DRAFT);
  },

  // Design review Z's scenarios.
  // Guard: the record's canaries are on screen before anything is deleted (so the assertions below can fail).
  async theRecordIsDrawn(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    assert.deepEqual(left(s).sort(), [QUEUED, ASKED, NOTE, MEMORY, 'Title Alpha'].sort());
    assert.equal(s.meters().length, 1, 'the budget bar');
    assert.equal(s.buttons('Remove').length, 1);
  },
  // Guard: a deletion that answers leaves it at once: nothing of it is on screen.
  async aDeletedConversationIsLeft(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(left(s), []);
    assert.deepEqual(s.selected(), ['Title Bravo']);
  },
  // r4-1 finding 1: the conversation is deleted, the DELETE's answer is lost, the record's reread answers 404: nothing
  // read from the record is on screen, and no Send, Edit or Remove of a waiting message.
  async nothingOfADeletedConversationStaysThoughItsAnswerWasLost(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog();
    assert.deepEqual(left(s), [], 'as the deletion settles');
    assert.equal(s.buttons('Remove').length + s.buttons('Edit').length + s.buttons('Send').length, 0, 'a waiting message can be acted on');
    await s.pause(); // the record is read again after a pause
    assert.deepEqual(left(s), [], 'after it was read again');
    // rider N (Harold, 2026-11-21): left once its record answered 404; what was typed there went with it
    assert.deepEqual(s.selected(), ['Title Bravo'], 'the deleted conversation is still open');
    assert.notEqual(s.draft(), DRAFT);
  },
  // This review: once the record has answered 404 nothing leaves for the deleted conversation (rider N: no view of it
  // is left).
  async nothingLeavesForAConversationKnownToBeDeleted(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog(); await s.pause();
    assert.deepEqual(s.selected(), ['Title Bravo'], 'rider N: no view of it is left to send from');
    await s.type(DRAFT); await s.send();
    assert.deepEqual(s.server.asked.filter((r) => r.path.includes('/conversations/a/')).map((r) => r.path), []);
  },
  // The same when the reread fails otherwise (the backend cannot be reached): nothing old after a failed read.
  async nothingOfADeletedConversationStaysWhileItCannotBeRead(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    for (const kind of ['conversation', 'spend', 'asks']) s.server.failing(kind, 'unreachable', 503);
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog();
    assert.deepEqual(left(s), []);
    assert.equal(s.buttons('Remove').length, 0);
  },
  // Finding 1, the project: the open project is deleted, the answer lost, the rereads 404.
  async nothingOfADeletedProjectStaysThoughItsAnswerWasLost(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    deleteProject(state, 'p1');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog(); await s.pause();
    assert.deepEqual(left(s).concat(/Project Alpha|Title (Bravo|Charlie)/.test(s.seen()) ? ['the project'] : []), []);
    assert.equal(s.buttons('Remove').length, 0);
  },
  // r4-1 finding 3, the project: its deletion is under way, the window is narrowed (the Sidebar and its dialog go) and
  // widened, a new conversation is started in it, the deletion answers: the deleted project is left.
  async aDeletedProjectIsLeftThoughAnotherVisitBegan(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    await goes(s);
    await s.newConversation();
    deleteProject(state, 'p1');
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(s.selected(), ['Title Zulu'], 'the project left for the other');
    await s.type(DRAFT); await s.send();
    assert.deepEqual(s.server.asked.filter((r) => r.body?.project_id === 'p1').map((r) => r.path), [], 'a message sent to the deleted project');
  },
  // Finding 3, the conversation: B is opened and then A again while A's deletion is under way.
  async aDeletedConversationIsLeftThoughAnotherVisitBegan(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    await goes(s);
    await s.open('Title Bravo'); await s.open('Title Alpha');
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(left(s), []);
    assert.deepEqual(s.selected(), ['Title Bravo']);
    await s.type(DRAFT); await s.send();
    assert.deepEqual(s.server.asked.filter((r) => r.path.includes('/conversations/a/')).map((r) => r.path), [], 'a message sent to the deleted conversation');
  },
  // Guard (X's rule): a deletion answering after another conversation was opened and typed in loses nothing typed there.
  async aDeletionAnsweredElsewhereLosesNothingTypedThere(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    await goes(s);
    await s.open('Title Bravo'); await s.type(DRAFT);
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.equal(s.draft(), DRAFT);
  },
  // Guard (X's rule, the project): the same with another project opened and typed in.
  async aProjectsDeletionAnsweredElsewhereLosesNothingTypedThere(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    await goes(s);
    await s.projectMenu(/^Project Beta$/); await s.type(DRAFT);
    deleteProject(state, 'p1');
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(s.selected(), ['Title Zulu']);
    assert.equal(s.draft(), DRAFT);
  },
  // This review: a turn of A is live (its stream open) when A is deleted and the answer is lost; the backend ends the
  // turn (its runs are revoked) and the record's reads answer 404: the live turn's question and answer are not on screen.
  async aDeletedConversationsLiveTurnIsGone(w, strict) {
    const state = withRecord(); state.records.a.queued = []; state.spend = {};
    const s = await world(w, strict, state);
    await s.type(LIVE); await s.send();
    const turn = s.server.next(writes('POST', '/message/stream')).open();
    await s.act(() => turn.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 2 }));
    assert.ok(s.seen().includes(LIVE), 'the live turn is drawn');
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog();
    await s.pause(); // the record is read again: 404
    assert.deepEqual(left(s), [], 'once its record answered 404');
    await s.act(() => turn.push({ type: 'tool_call', name: 'search' })); // an event of the turn still on its way
    assert.deepEqual(left(s), [], 'after a later event of its stream');
    await s.act(() => { turn.push({ type: 'run_finished', status: 'cancelled' }); turn.end(); });
    await s.pause(); await s.pause();
    assert.deepEqual(left(s), [], 'after its stream ended');
  },
  // This review: a turn of A is live when A's deletion answers: A is left, and live.js keeps nothing of it: its stream
  // is closed from here (the backend ends the turn too), so nothing of it is taken in after.
  async aDeletedConversationsStreamIsClosed(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(LIVE); await s.send();
    const request = s.server.next(writes('POST', '/message/stream')); const turn = request.open();
    await s.act(() => turn.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 2 }));
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    s.server.failing('conversation', 'unreachable', 503); // no read of its record gets through: the answer is what is known
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.equal(request.signal.aborted, true, 'its stream is still open here');
    assert.deepEqual(left(s), []);
  },
  // The same for a conversation that went with its project: a turn of A is live, B is opened, the project is deleted;
  // live.js asks for the conversation it holds, its record answers 404, and its stream is closed.
  async aDeletedProjectsLiveTurnElsewhereIsForgotten(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(LIVE); await s.send();
    const request = s.server.next(writes('POST', '/message/stream')); const turn = request.open();
    await s.act(() => turn.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 2 }));
    await s.open('Title Bravo');
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    deleteProject(state, 'p1');
    await s.release(s.server.next(writes('DELETE')), (r) => r.answer({ ok: true }));
    assert.deepEqual(s.selected(), ['Title Zulu']);
    assert.equal(request.signal.aborted, true, 'the stream of a conversation that went with the project is still open here');
  },
  // The same for a turn whose stream had ended while the record could not be read (it is shown until its record is
  // read: Harold, 2026-10-22); then the conversation is deleted, the answer lost, and the record answers 404.
  async aDeletedConversationsFinishedLiveTurnIsGone(w, strict) {
    const state = withRecord(); state.records.a.queued = []; state.spend = {};
    const s = await world(w, strict, state);
    await s.type(LIVE); await s.send();
    const turn = s.server.next(writes('POST', '/message/stream')).open();
    s.server.failing('conversation', 'unreachable', 503);
    await s.act(() => { turn.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 2 }, { type: 'run_finished', status: 'succeeded' }); turn.end(); });
    await s.pause();
    assert.ok(s.seen().includes(LIVE), 'the finished turn stays shown while its record cannot be read');
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a'); s.server.working('conversation');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog(); await s.pause();
    assert.deepEqual(left(s), []);
  },
  // This review: the deletion has happened and its answer has not come yet (a purge of the backups can take long); the
  // window was narrowed and widened, so no dialog covers the conversation; the researcher removes a waiting message,
  // which reads the record again: 404. Nothing read of the record stays, before the deletion's answer.
  async aDeletedConversationsRecordGoesAtItsOwn404(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    await goes(s);
    deleteConversation(state, 'a');
    await s.press('Remove');
    await s.release(s.server.next(writes('DELETE', '/queue/')), (r) => r.fail('not_found', 404));
    assert.deepEqual(left(s).filter((canary) => canary !== 'Title Alpha'), [], 'the record read before, after its 404'); // the list is the deletion's answer's
    assert.equal(s.buttons('Remove').length, 0);
  },
  // This review: the budget bar keeps what it read when its read fails (FollowUp.jsx); after the deletion its read
  // answers 404, or fails: the deleted conversation's spend is not on screen. (No waiting message here, so the bar is
  // what is tested.)
  async aDeletedConversationsBudgetBarIsGone(w, strict) {
    for (const how of ['404', 'unreachable']) {
      const state = withRecord(); state.records.a.queued = [];
      const s = await world(await load.fresh(), strict, state);
      assert.equal(s.meters().length, 1, 'the budget bar is drawn');
      await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
      deleteConversation(state, 'a');
      if (how === 'unreachable') for (const kind of ['conversation', 'spend', 'asks']) s.server.failing(kind, 'unreachable', 503);
      await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
      await s.closeDialog(); await s.pause();
      assert.equal(s.meters().length, 0, `the budget bar of the deleted conversation (${how})`);
    }
  },
  // Guard for a failed deletion that did not happen (S's point 4): the conversation stays, is read again and shown.
  async aDeletionThatDidNotHappenShowsTheRecordAgain(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('backup_failed', 503));
    await s.closeDialog();
    assert.deepEqual(s.selected(), ['Title Alpha']);
    assert.ok(s.seen().includes(QUEUED) && s.seen().includes(ASKED), 'read again and shown');
    assert.equal(s.draft(), DRAFT);
    assert.equal(s.meters().length, 1, 'the budget bar is back');
  },
  // The whole-batch review (F4): a turn of A is live, B is opened, A is deleted from the Sidebar and the answer is lost:
  // live.js asks for the conversation it holds, its record answers 404, and its stream is closed.
  async aDeletedConversationsLiveTurnElsewhereIsForgottenThoughItsAnswerWasLost(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(LIVE); await s.send();
    const request = s.server.next(writes('POST', '/message/stream')); const turn = request.open();
    await s.act(() => turn.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 2 }));
    await s.open('Title Bravo');
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.equal(request.signal.aborted, true, 'the stream of the deleted conversation is still open here');
  },
  // The recheck of the whole-batch review (F3): the budget bar is drawn with the turns, so not while the record is read
  // again after a change; and a failed read that follows no change keeps what was shown.
  async theBudgetBarWaitsForTheTurnsAfterAChange(w, strict) {
    const state = withRecord(); state.records.a.queued = []; const s = await world(w, strict, state);
    assert.equal(s.meters().length, 1);
    s.server.hold('conversation');
    await s.act(() => w.library.libraryChanged('p1'));
    assert.equal(s.meters().length, 0, 'a spend read before the change is shown while the record is read again');
    await s.releaseAll((r) => r.method === 'GET' && r.kind === 'conversation');
    assert.equal(s.meters().length, 1);
  },
  async aFailedReadWithNoChangeKeepsWhatWasShown(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.press('Remove');
    s.server.failing('conversation', 'unreachable', 503);
    await s.release(s.server.next(writes('DELETE', '/queue/')), (r) => r.answer({ ok: true })); // then read again: it fails
    assert.ok(s.seen().includes(ASKED), 'the turns read before were dropped at a failed read with no change');
  },
  // Rider N, its project half (Harold, 2026-11-21): the open project's deletion fails but happened; the Shell asks for
  // the project once, it answers 404, and the window leaves it.
  async aProjectWhoseFailedDeletionHappenedIsLeft(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    deleteProject(state, 'p1');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog();
    assert.deepEqual(s.selected(), ['Title Zulu'], 'the deleted project is still open');
    assert.ok(!/Project Alpha|Title (Alpha|Bravo|Charlie)/.test(s.seen()));
  },
  // Its guard (S's point 4): the project's deletion fails and did not happen: it answers, and the window stays, with
  // its record and what was typed.
  async aProjectWhoseFailedDeletionDidNotHappenStays(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('backup_failed', 503));
    await s.closeDialog();
    assert.deepEqual(s.selected(), ['Title Alpha']);
    assert.ok(s.seen().includes(QUEUED) && s.seen().includes(ASKED), 'read again and shown');
    assert.equal(s.draft(), DRAFT);
  },
  // Rider N with batch E's 1cc9511 and its recorded limit R2: after a deletion whose answer was lost, the waiting
  // messages stay while the record is read again (an open editor keeps its text through any change); when that read
  // answers 404 the window leaves the conversation.
  async aDeletedConversationsWaitingMessagesStayOnlyUntilItsRereadAnswers(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    deleteConversation(state, 'a');
    s.server.hold('conversation');
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog();
    assert.ok(s.seen().includes(QUEUED), 'R2: shown until the record is read again');
    assert.ok(!s.seen().includes(ASKED), 'the turns go at the change');
    s.server.free('conversation');
    await s.releaseAll((r) => r.method === 'GET' && r.kind === 'conversation');
    assert.deepEqual(s.selected(), ['Title Bravo'], 'left once its record answered 404 (rider N)');
    assert.deepEqual(left(s), []);
  },
  // Rider N before the deletion's answer: its record answers 404 (a waiting message's Remove reads it again) while the
  // deletion is still under way and its dialog went with the Sidebar: the window leaves it, and the list is read again,
  // so the deleted conversation is not offered to be opened again.
  async aConversationWhoseRecordAnswers404BeforeItsDeletionAnswersIsLeftAndUnlisted(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.menu('Title Alpha', 'Delete'); await s.confirmDelete();
    await goes(s);
    deleteConversation(state, 'a');
    await s.press('Remove');
    await s.release(s.server.next(writes('DELETE', '/queue/')), (r) => r.fail('not_found', 404));
    assert.deepEqual(s.selected(), ['Title Bravo']);
    assert.deepEqual(left(s), [], 'the deleted conversation is still listed');
  },
  // The rider N review (P2, P3): the project's read after its failed deletion gets no answer at first (the backend is
  // still down): it is asked again while the project is open, and the window leaves it once that read answers 404.
  async aProjectWhoseFailedDeletionHappenedIsLeftOnceItCanBeAsked(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    deleteProject(state, 'p1');
    for (const kind of ['project', 'conversation']) s.server.failing(kind, 'unreachable', 503);
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('unreachable', 503));
    await s.closeDialog(); await s.pause();
    for (const kind of ['project', 'conversation']) s.server.working(kind);
    await s.pause(); await s.pause();
    assert.deepEqual(s.selected(), ['Title Zulu'], 'the deleted project stays open once the backend answers again');
  },
  // Its guard: the project exists and its read fails at first: the window stays in it, with its record and what was
  // typed, however many times it is asked.
  async aProjectWhoseFailedDeletionDidNotHappenStaysThoughItCouldNotBeAsked(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    await s.type(DRAFT);
    await s.projectMenu(/^Delete /); await s.confirmDelete();
    s.server.failing('project', 'unreachable', 503);
    await s.release(s.server.next(writes('DELETE')), (r) => r.fail('backup_failed', 503));
    await s.closeDialog(); await s.pause();
    s.server.working('project'); await s.pause();
    assert.deepEqual(s.selected(), ['Title Alpha']);
    assert.ok(s.seen().includes(QUEUED) && s.seen().includes(ASKED));
    assert.equal(s.draft(), DRAFT);
  },
  // The recheck of the rider N review (findings 1 to 3): the project's read after a failed deletion is asked again only
  // while there is no answer, only while that project is open and the Shell is drawn, and as one chain per project.
  async aProjectAskedAgainStopsWhenItIsLeftAnswersOrTheShellGoes(w, strict) {
    const state = withRecord(); const s = await world(w, strict, state);
    const reads = () => s.server.asked.filter((r) => r.method === 'GET' && r.path === '/api/projects/p1');
    const failDeletion = async () => {
      await s.projectMenu(/^Delete /); await s.confirmDelete();
      await s.release(s.server.next(writes('DELETE')), (r) => r.fail('backup_failed', 503));
      await s.closeDialog();
    };
    s.server.hold('project');
    await failDeletion(); await failDeletion(); // two failed deletions of the open project, each asking for it
    assert.equal(reads().length, 2);
    for (const read of reads()) await s.release(s.server.next((r) => r === read), (r) => r.fail('unreachable', 0)); // no answer
    await s.pause();
    assert.equal(reads().length, 1, 'one chain per project, asked again after a pause');
    await s.release(s.server.next((r) => r.path === '/api/projects/p1'), (r) => r.fail('forbidden', 403)); // an answer
    await s.pause();
    assert.equal(reads().length, 0, 'asked again after an answer that is not a 404');
    await failDeletion();
    await s.release(s.server.next((r) => r.path === '/api/projects/p1'), (r) => r.fail('unreachable', 503));
    await s.projectMenu(/^Project Beta$/); // left while it is asked again
    await s.pause();
    assert.deepEqual(reads().map((r) => r.path), [], 'asked again though the project was left');
    await s.projectMenu(/^Project Alpha$/);
    await s.releaseAll((r) => r.path === '/api/projects/p1'); // the project's own read as it opens (not the deletion's)
    await failDeletion();
    await s.release(s.server.next((r) => r.path === '/api/projects/p1'), (r) => r.fail('database_damaged', 503));
    await s.act(() => s.ui.show([])); // the window is replaced (App.jsx's limited state): the Shell goes
    await s.pause(); await s.pause();
    assert.equal(reads().length, 0, 'asked again after the Shell went');
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
