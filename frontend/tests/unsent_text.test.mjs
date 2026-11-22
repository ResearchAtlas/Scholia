// Design review X's scenarios (PR 63, its appendix A1: unsent or unsaved text lost when an answer lands): the real
// Shell with its Sidebar and ConversationView, the real Library with its Paper, and the real memory views, on the stand-in (harness/standin.mjs).
// Every answer is released by the scenario; no timer runs by itself; no sleeps. Each asserts on what the drawn tree says.
// A field that is read-only or disabled takes no typing (typeInto returns false): a scenario then asserts that nothing
// could be typed, which is the other way of losing nothing.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const clone = (value) => structuredClone(value);
const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
function backend(state) {
  const asked = []; const held = new Set();
  const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const query = (path, name = 'project_id') => new URLSearchParams(path.split('?')[1]).get(name);
  globalThis.fetch = (path, init = {}) => {
    const method = init.method ?? 'GET';
    const kind = method !== 'GET' ? 'write' : path === '/api/projects' ? 'projects'
      : path.startsWith('/api/conversations?') ? 'list' : /^\/api\/conversations\/[^/?]+$/.test(path) ? 'conversation'
        : path.startsWith('/api/asks') ? 'asks' : path.startsWith('/api/settings?') ? 'settings'
          : path === '/api/settings' ? 'choice' : path.startsWith('/api/memory') ? 'memory'
            : /\/materials$/.test(path) ? 'materials' : 'other';
    const now = () => ({ projects: { projects: clone(state.projects ?? []) },
      list: { conversations: clone(state.conversations?.[query(path)] ?? []) },
      conversation: clone(state.records?.[path.split('/').pop()] ?? { turns: [], queued: [] }),
      asks: { asks: clone(state.asks?.[query(path, 'conversation_id')] ?? []), working: 0 },
      choice: { values: clone(state.personal ?? {}), hash: 'h' },
      settings: { hash: 'h', values: { ui: { panel: 'none' } } },
      memory: clone(state.memory ?? { personal: [], project: [] }),
      materials: { materials: clone(state.papers ?? []), asks: [] },
    }[kind] ?? { ok: true });
    if (method === 'GET' && state.gets && path in state.gets) return Promise.resolve(response(200, clone(state.gets[path])));
    if (kind === 'other') return new Promise(() => {});
    if (method === 'GET' && !held.has(kind)) return Promise.resolve(response(200, now()));
    return new Promise((resolve, reject) => { init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))); // as fetch does
      asked.push({ method, path, kind, body: init.body ? JSON.parse(init.body) : null,
      answer: (body = now(), status = 200) => resolve(response(status, body)),
      fail: (code = 'internal', status = 500) => resolve(response(status, { code })),
      // a turn's stream, opened: push(...events) sends them, end() closes it (the turn's stream ended)
      open() { let controller; resolve({ ok: true, status: 200, body: new ReadableStream({ start(c) { controller = c; } }) });
        return { push: (...events) => { for (const event of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)); },
          end: () => controller.close(), lose: () => controller.error(new TypeError('network')) }; } }); });
  };
  globalThis.FileReader = class { readAsDataURL(file) { this.result = `data:;base64,${file.data ?? ''}`; queueMicrotask(() => this.onload()); } };
  globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = () => 0;
  globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400 };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  return { asked, hold: (kind) => held.add(kind), free: (kind) => held.delete(kind),
    next: (test) => { const i = asked.findIndex(test); if (i < 0) throw new Error(`nothing asked: ${asked.map((r) => `${r.method} ${r.path}`).join(', ')}`); return asked.splice(i, 1)[0]; } };
}

// What is drawn, and the researcher's hands on it.
function hands(ui, server) {
  const off = (x) => x.type === 'drawer:Root' && !x.props.open; // the drawer's copy of the sidebar, closed
  const elements = () => { const out = []; const stack = [];
    for (const h of ui.hosts()) {
      if (h.props) { const e = { type: h.type, props: h.props, text: [], up: [...stack] }; out.push(e); stack.push(e); }
      else if (h.end) stack.pop(); else for (const e of stack) e.text.push(h.text);
    }
    return out.map((e) => ({ ...e, text: e.text.join(' ') })).filter((e) => ![e, ...e.up].some(off)); };
  const hidden = (e) => [e, ...e.up].some((x) => (x.type === 'ui:Dialog' && !x.props.open) || x.type === 'ui:DropdownMenuContent');
  const shown = () => elements().filter((e) => !hidden(e));
  const act = async (fn) => { fn(); await ui.settle(); };
  const one = (found, what) => { if (found.length !== 1) throw new Error(`${what}: ${found.length} found`); return found[0]; };
  const locked = (e) => Boolean(e.props.readOnly || e.props.disabled || e.up.some((x) => x.type === 'fieldset' && x.props.disabled));
  const fields = (test) => shown().filter((e) => ['textarea', 'input', 'ui:Input', 'ui:Textarea'].includes(e.type) && e.props.onChange && test(e));
  const api = {
    ui, server, elements, shown, act,
    screen: () => shown().filter((e) => !e.up.length).map((e) => e.text).join(' '),
    field: (test, what = 'field') => one(fields(test), what),
    has: (test) => fields(test).length > 0,
    // types as the researcher can: a read-only or disabled field takes nothing; whether it took it
    typeInto: async (test, value, what) => { const e = api.field(test, what); if (locked(e)) return false;
      await act(() => e.props.onChange({ target: { value } })); return true; },
    box: () => api.field((e) => e.type === 'textarea' && e.props.id === 'composer', 'message box').props.value,
    type: (value) => api.typeInto((e) => e.type === 'textarea' && e.props.id === 'composer', value, 'message box'),
    send: () => act(() => { one(shown().filter((e) => e.type === 'form' && e.props.className?.includes('pb-4')), 'message form').props.onSubmit({ preventDefault() {} }); }),
    submitWith: (test, what) => act(() => { one(shown().filter((e) => e.type === 'form' && e.props.onSubmit && fields((f) => f.up.some((x) => x.props === e.props) && test(f)).length), what)
      .props.onSubmit({ preventDefault() {} }); }),
    press: (label, within = () => true) => act(() => one(shown().filter((e) => (e.type === 'ui:Button' || e.type === 'button') && e.props.onClick && e.text.trim() === label && within(e)), label)
      .props.onClick({ preventDefault() {}, currentTarget: null })),
    menu: (title, item) => act(() => one(elements().filter((e) => e.type === 'ui:DropdownMenuItem' && e.text.trim() === item && e.up.some((x) => x.type === 'li' && x.text.join(' ').includes(title))), `${title}: ${item}`).props.onSelect()),
    projectMenu: (item) => act(() => one(elements().filter((e) => e.type === 'ui:DropdownMenuItem' && item.test(e.text.trim())), String(item)).props.onSelect()),
    newConversation: () => act(() => elements().filter((e) => e.type === 'ui:Button' && e.text.trim() === 'New conversation')[0].props.onClick()),
    says: (role) => shown().filter((e) => e.props.role === role).map((e) => e.text),
    release: (request, how = (r) => r.answer()) => act(() => how(request)),
    // a read takes longer than a draw: reads of this kind asked from now on are held, drawn without, then answered
    releaseAll: async (test, how = (r) => r.answer()) => { while (server.asked.some(test)) await api.release(server.next(test), how); },
  };
  return api;
}

async function windowWorld({ mount, jsx }, strict, state) {
  const server = backend(state);
  const { Shell } = await load.component('Shell');
  const ui = await mount([jsx(Shell, { health: {}, settings: { values: {} }, onLanguage() {} })], { strict }).settle();
  return hands(ui, server);
}
async function memoryWorld({ mount, jsx }, strict, state) {
  const server = backend(state);
  const { MemorySection } = await load.component('Memory');
  const ui = await mount([jsx(MemorySection, { project: { id: 'p1' } })], { strict }).settle();
  return hands(ui, server);
}
async function libraryWorld({ mount, jsx, Library }, strict, state) {
  const server = backend(state);
  const ui = await mount([jsx(Library, { project: { id: 'p1' } }, 'p1')], { strict }).settle();
  return hands(ui, server);
}

async function settingsWorld({ mount, jsx }, strict, state, props) {
  const server = backend(state);
  const { Settings } = await load.component('Settings');
  const draw = (more = {}) => jsx(Settings, { open: true, onOpenChange() {}, health: {}, project: null, onLanguage() {}, ...props, ...more });
  const ui = await mount([draw()], { strict }).settle();
  const s = hands(ui, server);
  s.redraw = async (more) => { ui.show([draw(more)]); await ui.settle(); }; // the Shell drawing it again with new props
  s.page = () => s.shown().filter((e) => e.type === 'button' && e.props['aria-current'] === 'page').map((e) => e.text.trim());
  return s;
}

async function partWorld({ mount, jsx }, strict, state, file, name, props = {}) {
  const server = backend(state);
  const part = (await load.component(file))[name];
  const ui = await mount([jsx(part, props)], { strict }).settle();
  return hands(ui, server);
}

const P1 = { id: 'p1', name: 'Project Alpha', kind: 'research' };
const conv = (id, title) => ({ id, title, running: false });
const start = (more = {}) => ({ projects: [P1], conversations: { p1: [conv('a', 'Title Alpha'), conv('b', 'Title Bravo')] }, ...more });
const writes = (method, part = '') => (r) => r.method === method && r.path.includes(part);
const reads = (kind) => (r) => r.method === 'GET' && r.kind === kind;
const FIRST = 'FIRST-SENT-CANARY';
const DRAFT = 'UNSENT-CANARY-DRAFT';
const SUBMITTED = 'SUBMITTED-CANARY';
const NEWER = 'NEWER-UNSAVED-CANARY';
const record = (id, content, status = 'confirmed') => ({ id, content, type: 'fact', status, version: 1, sources: [], valid_from: null, structured: {} });
const paper = (id, title) => ({ id, title, state: 'ready', csl: {}, source: 'upload', version: { id: `v-${id}`, media_type: 'application/pdf', size: 10, seq: 0 },
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', readable: true, retraction: null, extraction: null });
const byId = (part) => (e) => String(e.props.id ?? '').includes(part);

// An editor whose save is under way, then answered: nothing typed since the press is lost. Either the field took
// nothing while its save was under way, or what it took is still in a field after the answer.
async function nothingTypedSinceIsLost(s, test, what, answer) {
  const typed = await s.typeInto(test, NEWER, what);
  await answer();
  if (typed) assert.ok(s.has((e) => e.props.value === NEWER), `${what}: typed while its request was under way, gone after the answer`);
  return typed;
}

// ---- design review AF's scenarios: unsent text against a parent's remount and an earlier form's answer ----
const button = (s, label) => one(s.shown().filter((e) => (e.type === 'ui:Button' || e.type === 'button') && e.props.onClick && e.text.trim() === label), label);
// presses as the researcher can: a disabled control takes no press; whether it took it
const pressIfCan = async (s, label) => { if (button(s, label).props.disabled) return false; await s.press(label); return true; };
// a part drawn alone, with something done between the world's globals and the first draw (a timer kept, a picker given)
async function drawn({ mount, jsx }, strict, state, file, name, props, prepare = () => {}) {
  const server = backend(state);
  prepare();
  const part = (await load.component(file))[name];
  const draw = (more = {}) => jsx(part, { ...props, ...more });
  const ui = await mount([draw()], { strict }).settle();
  const s = hands(ui, server);
  s.redraw = async (more) => { ui.show([draw(more)]); await ui.settle(); };
  return s;
}
const lab = (more = {}) => ({ name: 'lab', kind: 'openai-compatible', base_url: 'https://llm.example.org/v1', enabled: true, has_key: false, ...more });
const helper = (installed) => ({ models: [{ id: 'm', name: 'Search model', size: 10, license: 'MIT', installed,
  sources: { huggingface: 'https://hf.example/m' }, sha256: 'ab', folder: '/models', file: 'model.gguf' }], download: null,
  helper: { state: 'stopped' }, search: installed ? { mode: 'hybrid' } : { mode: 'keyword_only', reason: 'model_missing' } });
const inForm = (e) => e.type === 'ui:Input' && e.up.some((x) => x.type === 'form');
const KEY_STORE = 'key store was unavailable';


// recheck r9's picker scenarios: two models that each need a window, the picker open
const picking = () => ({ personal: {}, gets: {
  '/api/providers': { providers: [lab({ has_key: true })] }, '/api/models/recent': { models: [] },
  '/api/providers/lab/models': { status: {}, models: [
    { id: 'm1', name: 'Model One', offered: true, window: { status: 'needed' }, effort: { steps: [] } },
    { id: 'm2', name: 'Model Two', offered: true, window: { status: 'needed' }, effort: { steps: [] } }] } } });
const option = (s, name) => one(s.shown().filter((e) => e.type === 'button' && e.props.role === 'option' && e.text.includes(name)), name);
const windowFor = async (s, name, value) => {
  await s.act(() => one(s.elements().filter((e) => e.type === 'ui:Popover'), 'the picker').props.onOpenChange(true));
  await s.act(() => option(s, name).props.onClick());
  await s.typeInto(byId('picker-window'), value, 'the window');
  await s.act(() => s.field(byId('picker-window'), 'the window').props.onKeyDown({ key: 'Enter', preventDefault() {} }));
};

const scenarios = {
  // r3-1 and r3-2, finding 1: a message is sent and its request fails after the researcher typed the next one: the box
  // holds what was typed since, and what was sent is given back.
  async aFailedSendKeepsWhatWasTypedSince(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.type(FIRST); await s.send();
    assert.equal(s.box(), '');
    await s.type(DRAFT);
    await s.release(s.server.next(writes('POST', '/message/stream')), (r) => r.fail());
    assert.ok(s.box().includes(DRAFT), `what was typed since is gone: ${JSON.stringify(s.box())}`);
    assert.ok(`${s.box()} ${s.screen()}`.includes(FIRST), 'what was sent is given back, in the box or beside it');
    assert.equal(s.says('alert').length, 1, 'the failure is said');
  },

  // The same in a new conversation, whose creation fails.
  async aFailedFirstSendKeepsWhatWasTypedSince(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.newConversation();
    await s.type(FIRST); await s.send();
    await s.type(DRAFT);
    await s.release(s.server.next(writes('POST', '/api/conversations')), (r) => r.fail());
    assert.ok(s.box().includes(DRAFT), `what was typed since is gone: ${JSON.stringify(s.box())}`);
    assert.ok(`${s.box()} ${s.screen()}`.includes(FIRST), 'what was sent is given back, in the box or beside it');
  },

  // The same while a turn runs: the message steers, and the steering request fails.
  async aFailedSteerKeepsWhatWasTypedSince(w, strict) {
    const s = await windowWorld(w, strict, start({ records: { a: { queued: [],
      turns: [{ run_id: 'r0', status: 'running', message: { text: 'Earlier question' }, answer: null }] } } }));
    await s.type(FIRST); await s.send();
    await s.type(DRAFT);
    await s.release(s.server.next(writes('POST', '/steering')), (r) => r.fail());
    assert.ok(s.box().includes(DRAFT), `what was typed since is gone: ${JSON.stringify(s.box())}`);
    assert.ok(`${s.box()} ${s.screen()}`.includes(FIRST), 'what was sent is given back, in the box or beside it');
  },

  // Guard (green at the head): a failed send with nothing typed since puts the message back in the box.
  async aFailedSendWithNothingTypedSinceIsBackInTheBox(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.type(FIRST); await s.send();
    await s.release(s.server.next(writes('POST', '/message/stream')), (r) => r.fail());
    assert.equal(s.box(), FIRST);
  },

  // Guard (green at the head; design review U): what is typed while a message is admitted stays, and can be typed.
  async typedWhileAMessageIsAdmittedIsKept(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.type(FIRST); await s.send();
    assert.equal(await s.type(DRAFT), true, 'the message box takes typing while a message is admitted');
    const stream = s.server.next(writes('POST', '/message/stream')).open();
    await s.act(() => stream.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 1 }));
    assert.equal(s.box(), DRAFT);
  },

  // Recheck r4 (pass 2, finding 1), against X's decided text ("the same when Stop was pressed since the send"): a message
  // whose request Stop aborted before it was admitted resolved as admitted, so it left the box and was never recorded.
  // Now it is not sent: the box holds it, a line break, then what was typed since. In an existing and a new conversation.
  async aSendStoppedBeforeItsAdmissionIsGivenBack(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.type(FIRST); await s.send();
    s.server.next(writes('POST', '/message/stream')); // asked, never answered: not admitted
    await s.type(DRAFT);
    await s.act(() => one(s.shown().filter((e) => e.type === 'ui:Button' && e.props['aria-label'] === 'Stop'), 'Stop').props.onClick());
    assert.equal(s.box(), `${FIRST}\n${DRAFT}`);
    assert.equal(s.says('alert').length, 0, 'a failure was said for a Stop'); // the whole-batch review (F2)
  },
  async aFirstSendStoppedBeforeItsAdmissionIsGivenBack(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.newConversation();
    await s.type(FIRST); await s.send();
    await s.release(s.server.next(writes('POST', '/api/conversations')), (r) => r.answer({ id: 'n1' }));
    s.server.next(writes('POST', '/api/conversations/n1/message/stream'));
    await s.type(DRAFT);
    await s.act(() => one(s.shown().filter((e) => e.type === 'ui:Button' && e.props['aria-label'] === 'Stop'), 'Stop').props.onClick());
    assert.equal(s.box(), `${FIRST}\n${DRAFT}`);
    assert.equal(s.says('alert').length, 0, 'a failure was said for a Stop'); // the whole-batch review (F2)
  },
  // Its controls: a message admitted (the run started) was sent, whether its stream is then lost or Stop is pressed: it
  // is not given back, the turn it started stays shown, and no failure is said.
  async anAdmittedSendWhoseStreamIsLostIsNotGivenBack(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.type(FIRST); await s.send();
    const stream = s.server.next(writes('POST', '/message/stream')).open();
    await s.type(DRAFT);
    await s.act(() => stream.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 1 }));
    await s.act(() => stream.lose());
    assert.equal(s.box(), DRAFT);
    assert.ok(s.screen().includes(FIRST), 'the turn it started is no longer shown');
    assert.equal(s.says('alert').length, 0);
  },
  async anAdmittedSendStoppedIsNotGivenBack(w, strict) {
    const s = await windowWorld(w, strict, start());
    await s.type(FIRST); await s.send();
    const stream = s.server.next(writes('POST', '/message/stream')).open();
    await s.type(DRAFT);
    await s.act(() => stream.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 1 }));
    await s.act(() => one(s.shown().filter((e) => e.type === 'ui:Button' && e.props['aria-label'] === 'Stop'), 'Stop').props.onClick());
    assert.ok(s.server.asked.some(writes('POST', '/api/runs/r1/cancel')), 'Stop asked the run to stop');
    await s.act(() => { stream.push({ type: 'run_finished', status: 'cancelled' }); stream.end(); });
    assert.equal(s.box(), DRAFT);
    assert.equal(s.says('alert').length, 0);
  },

  // r3-2, finding 1, first site: a waiting message's edit is saved, and more is typed while the save is under way.
  async aQueuedEditSavedLosesNothingTypedSince(w, strict) {
    const state = start({ records: { a: { turns: [], queued: [{ id: 'q1', text: 'Waiting message' }] } } });
    const s = await windowWorld(w, strict, state);
    await s.press('Edit');
    assert.equal(await s.typeInto(byId('queued-q1'), SUBMITTED, 'the edit'), true);
    await s.submitWith(byId('queued-q1'), 'the edit form');
    const put = s.server.next(writes('PUT', '/queue/q1'));
    assert.equal(put.body.content, SUBMITTED);
    state.records.a.queued[0].text = SUBMITTED;
    await nothingTypedSinceIsLost(s, byId('queued-q1'), 'a waiting message\'s edit', () => s.release(put, (r) => r.answer({ id: 'q1', text: SUBMITTED })));
  },

  // Guard (green at the head): saved with nothing typed since, the editor closes and the saved text is shown.
  async aQueuedEditSavedClosesItsEditor(w, strict) {
    const state = start({ records: { a: { turns: [], queued: [{ id: 'q1', text: 'Waiting message' }] } } });
    const s = await windowWorld(w, strict, state);
    await s.press('Edit');
    await s.typeInto(byId('queued-q1'), SUBMITTED, 'the edit');
    await s.submitWith(byId('queued-q1'), 'the edit form');
    state.records.a.queued[0].text = SUBMITTED;
    await s.release(s.server.next(writes('PUT', '/queue/q1')), (r) => r.answer({ id: 'q1', text: SUBMITTED }));
    assert.ok(!s.has(byId('queued-q1')), 'the editor is closed');
    assert.ok(s.screen().includes(SUBMITTED));
  },

  // r3-2, finding 1, second and third sites: a memory record's edit, and a proposal's.
  async aMemoryEditSavedLosesNothingTypedSince(w, strict) {
    const state = { memory: { personal: [], project: [record('m1', 'A record')] } };
    const s = await memoryWorld(w, strict, state);
    await s.press('Edit');
    await s.typeInto(byId('memory-m1'), SUBMITTED, 'the edit');
    await s.submitWith(byId('memory-m1'), 'the edit form');
    const patch = s.server.next(writes('PATCH', '/api/memory/m1'));
    state.memory.project = [{ ...record('m1', SUBMITTED), version: 2 }];
    s.server.hold('memory');
    await nothingTypedSinceIsLost(s, byId('memory-m1'), 'a memory record\'s edit', async () => {
      await s.release(patch, (r) => r.answer({ id: 'm1', version: 2 })); await s.releaseAll(reads('memory')); });
  },

  async aProposalEditSavedLosesNothingTypedSince(w, strict) {
    const state = { memory: { personal: [], project: [record('m2', 'A proposal', 'proposed')] } };
    const s = await memoryWorld(w, strict, state);
    await s.press('Edit');
    await s.typeInto(byId('proposal-m2'), SUBMITTED, 'the edit');
    await s.submitWith(byId('proposal-m2'), 'the edit form');
    const post = s.server.next(writes('POST', '/api/memory/review'));
    state.memory.project = [{ ...record('m2', SUBMITTED), version: 2 }];
    s.server.hold('memory');
    await nothingTypedSinceIsLost(s, byId('proposal-m2'), 'a proposal\'s edit', async () => {
      await s.release(post, (r) => r.answer({ id: 'm2', version: 2 })); await s.releaseAll(reads('memory')); });
  },

  // As decided (Harold, 2026-11-11, PR 51 Q16): a memory change anywhere reads the views again, which closes every open
  // editor. Kept as a scenario so it is not found again as new, and not "fixed" by the way.
  async aMemoryChangeElsewhereClosesAnOpenEditorAsDecided(w, strict) {
    const state = { memory: { personal: [], project: [record('m1', 'A record'), record('m3', 'Another record')] } };
    const s = await memoryWorld(w, strict, state);
    await s.act(() => s.shown().filter((e) => e.type === 'ui:Button' && e.text.trim() === 'Edit')[0].props.onClick());
    await s.typeInto(byId('memory-m1'), NEWER, 'the edit');
    s.server.hold('memory');
    await s.act(() => w.library.libraryEvents.dispatchEvent(new CustomEvent('changed', { detail: null }))); // memory.js memoryChanged(null)
    await s.releaseAll(reads('memory'));
    assert.ok(s.screen().includes('Another record'), 'read again');
    assert.ok(!s.has(byId('memory-m1')), 'the editor is closed by the reread');
  },

  // This review (r3-2 lists it as holding): a paper's details are saved, and another field is edited while the save
  // is under way; the save's own answer draws the page again.
  async aPapersDetailsSavedLoseNothingTypedSince(w, strict) {
    const state = { papers: [paper('a', 'Paper A'), paper('b', 'Paper B')] };
    const s = await libraryWorld(w, strict, state);
    await s.press('Paper A');
    await s.typeInto(byId('-title'), SUBMITTED, 'title');
    await s.submitWith(byId('-title'), 'details');
    const patch = s.server.next(writes('PATCH', '/api/materials/a'));
    state.papers[0] = { ...state.papers[0], title: SUBMITTED, updated_at: '2026-01-02T00:00:00Z' };
    await nothingTypedSinceIsLost(s, byId('-venue'), 'a paper\'s details', () => s.release(patch, (r) => r.answer({ ok: true })));
  },

  // As decided (Harold, 2026-11-14, design review F's option 2.2): a change to the papers from elsewhere draws the page
  // again and its unsaved details go. Kept as a scenario so it is not found again as new.
  async aLibraryChangeElsewhereDrawsThePageAgainAsDecided(w, strict) {
    const state = { papers: [paper('a', 'Paper A'), paper('b', 'Paper B')] };
    const s = await libraryWorld(w, strict, state);
    await s.press('Paper A');
    await s.typeInto(byId('-venue'), NEWER, 'venue');
    await s.act(() => w.library.libraryChanged('p1'));
    assert.equal(s.field(byId('-venue'), 'venue').props.value, '', 'drawn again from the saved details');
  },

  // This review: the Sidebar's dialogs close on their request's answer over what was typed while it was under way.
  async aRenameSavedLosesNothingTypedSince(w, strict) {
    const state = start();
    const s = await windowWorld(w, strict, state);
    await s.menu('Title Bravo', 'Rename');
    await s.typeInto(byId('rename'), SUBMITTED, 'title');
    await s.submitWith(byId('rename'), 'rename');
    const put = s.server.next(writes('PUT', '/api/conversations/b'));
    state.conversations.p1[1].title = SUBMITTED;
    const typed = await s.typeInto(byId('rename'), NEWER, 'title');
    await s.release(put);
    assert.equal(typed, false, 'the title could be changed while its save was under way, and the dialog then closed over it');
  },

  async aNewProjectMadeLosesNothingTypedSince(w, strict) {
    const state = start();
    const s = await windowWorld(w, strict, state);
    await s.projectMenu(/^New project$/);
    await s.typeInto(byId('new-project'), SUBMITTED, 'name');
    await s.submitWith((e) => e.props.id === 'new-project', 'new project');
    const post = s.server.next(writes('POST', '/api/projects'));
    const typed = await s.typeInto((e) => e.props.id === 'new-project', NEWER, 'name');
    state.projects = [P1, { id: 'p3', name: SUBMITTED, kind: 'research' }];
    await s.release(post, (r) => r.answer({ id: 'p3', name: SUBMITTED, kind: 'research' }));
    assert.equal(typed, false, 'the name could be changed while the project was made, and the dialog then closed over it');
  },

  // This review: a waiting message is being edited when the turn before it ends; the window starts the message on its
  // own, as it was, and the edit goes with its row.
  async aQueuedEditIsNotLostWhenItsMessageStartsOnItsOwn(w, strict) {
    const state = start({ personal: { ui: { follow_up: 'queue' } }, records: { a: { turns: [], queued: [] } } });
    const s = await windowWorld(w, strict, state);
    await s.type('First question'); await s.send();
    const first = s.server.next(writes('POST', '/message/stream')).open();
    state.records.a = { queued: [], turns: [{ run_id: 'r1', status: 'running', message: { text: 'First question' }, answer: null }] };
    await s.act(() => first.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 1 }));
    await s.type('Waiting message'); await s.send(); // it waits as the next turn
    state.records.a.queued = [{ id: 'q1', text: 'Waiting message' }];
    await s.release(s.server.next(writes('POST', '/queue')), (r) => r.answer({ id: 'q1', text: 'Waiting message' }));
    await s.press('Edit');
    assert.equal(await s.typeInto(byId('queued-q1'), NEWER, 'the edit'), true);
    // the turn ends well; the window reads the record and starts the oldest waiting message
    state.records.a.turns[0] = { ...state.records.a.turns[0], status: 'succeeded', answer: { text: 'An answer' }, result_saved: true };
    await s.act(() => { first.push({ type: 'run_finished', status: 'succeeded' }); first.end(); });
    const started = s.server.asked.filter(writes('POST', '/queue/q1/send'));
    if (started.length) { // started on its own, with the words it had before the edit
      const second = s.server.next(writes('POST', '/queue/q1/send')).open();
      state.records.a = { queued: [], turns: [...state.records.a.turns, { run_id: 'r2', status: 'running', message: { text: 'Waiting message' }, answer: null }] };
      await s.act(() => second.push({ type: 'run_started', run_id: 'r2', conversation_id: 'a', seq: 1 }));
    }
    assert.ok(s.has((e) => e.props.value === NEWER), `the edit typed is gone${started.length ? ', and its message was started as it was before the edit' : ''}`);
  },
  // Rider Q's other half: the message held by its open editor starts, as the record then says, once the editor closes.
  async aHeldMessageStartsOnceItsEditorCloses(w, strict) {
    const state = start({ personal: { ui: { follow_up: 'queue' } }, records: { a: { turns: [], queued: [] } } });
    const s = await windowWorld(w, strict, state);
    await s.type('First question'); await s.send();
    const first = s.server.next(writes('POST', '/message/stream')).open();
    state.records.a = { queued: [], turns: [{ run_id: 'r1', status: 'running', message: { text: 'First question' }, answer: null }] };
    await s.act(() => first.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 1 }));
    await s.type('Waiting message'); await s.send();
    state.records.a.queued = [{ id: 'q1', text: 'Waiting message' }];
    await s.release(s.server.next(writes('POST', '/queue')), (r) => r.answer({ id: 'q1', text: 'Waiting message' }));
    await s.press('Edit');
    await s.typeInto(byId('queued-q1'), NEWER, 'the edit');
    state.records.a.turns[0] = { ...state.records.a.turns[0], status: 'succeeded', answer: { text: 'An answer' }, result_saved: true };
    await s.act(() => { first.push({ type: 'run_finished', status: 'succeeded' }); first.end(); });
    assert.equal(s.server.asked.filter(writes('POST', '/queue/q1/send')).length, 0, 'started while its editor was open');
    await s.submitWith(byId('queued-q1'), 'the edit form'); // saved: the editor closes
    state.records.a.queued = [{ id: 'q1', text: NEWER }];
    await s.release(s.server.next(writes('PUT', '/queue/q1')), (r) => r.answer({ id: 'q1', text: NEWER }));
    assert.equal(s.server.asked.filter(writes('POST', '/queue/q1/send')).length, 1, 'started once its editor closed');
    assert.ok(s.screen().includes(NEWER), 'with the words saved');
  },

  // This review: an answer typed to a question of the agent's, and a change to the papers or to memory (an attach
  // ending, a turn's memory event) drops the questions shown and reads them again: the typed answer goes with them.
  async anAnswerTypedToAQuestionIsKeptWhenThePapersChange(w, strict) {
    const ask = { ask_id: 'k1', run_id: 'r9', kind: 'agent', text_box: true, options: ['1'], params: { question: 'Which year?' },
      project_kind: 'research', project_name: 'Project Alpha' };
    const s = await windowWorld(w, strict, start({ asks: { a: [ask] } }));
    const mine = (e) => e.type === 'ui:Input' && e.up.some((x) => x.props.role === 'group');
    assert.equal(await s.typeInto(mine, DRAFT, 'the answer'), true);
    s.server.hold('asks');
    await s.act(() => w.library.libraryChanged('p1'));
    await s.releaseAll(reads('asks'));
    assert.ok(s.screen().includes('Which year?'), 'the question is shown again once read');
    assert.equal(s.field(mine, 'the answer').props.value, DRAFT);
  },

  // Rider P (X has no scenario: its harness drew no Settings; this one draws Settings alone, as the Shell draws it): a
  // running turn's open_place for a Settings page, while Settings is open at another page with something typed there,
  // leaves the page and what was typed.
  async aTurnsOpenPlaceLeavesAnOpenSettingsPage(w, strict) {
    const s = await settingsWorld(w, strict, { personal: { ui: { language: 'en' }, subagents: { models: [] } } }, { open: false, startPage: null });
    await s.redraw({ open: true, startPage: 'subagents' }); // opened at Subagents (the stand-in's settings answer draws it)
    assert.equal(await s.typeInto(byId('subagent-add'), NEWER, 'a model to add'), true);
    await s.redraw({ open: true, startPage: 'project' }); // live.js openPlace('settings.project'): Shell.jsx sets the start page
    assert.deepEqual(s.page(), ['Subagents'], 'the page the researcher had open was changed');
    assert.equal(s.field(byId('subagent-add'), 'a model to add').props.value, NEWER);
  },
  // Guard (S1-19c, green at the head): Settings opened by open_place opens at the page named.
  async aClosedSettingsOpenedByATurnOpensAtItsPage(w, strict) {
    const s = await settingsWorld(w, strict, { personal: { ui: { language: 'en' }, subagents: { models: [] } } }, { open: false, startPage: null });
    await s.redraw({ open: true, startPage: 'subagents' });
    assert.deepEqual(s.page(), ['Subagents']);
  },

  // The batch's internal review (F2), rider Q's other statement: a waiting message's own Send (which sends the text the
  // record holds, not the edit) cannot be pressed while its editor is open.
  async aWaitingMessagesSendWaitsForItsEditor(w, strict) {
    const s = await windowWorld(w, strict, start({ records: { a: { turns: [], queued: [{ id: 'q1', text: 'Waiting message' }] } } }));
    const send = () => s.shown().filter((e) => e.type === 'ui:Button' && e.text.trim() === 'Send' && e.props.className?.includes('h-7'));
    assert.equal(send().length, 1);
    assert.ok(!send()[0].props.disabled);
    await s.press('Edit');
    await s.typeInto(byId('queued-q1'), NEWER, 'the edit');
    assert.ok(send()[0].props.disabled, 'the row\'s Send would send the unedited text and discard the edit');
  },

  // The whole batch E review (F1, P2): design review Z's one record dropped the waiting messages at every change to the
  // papers or to memory (a turn's memory note is one), so an open editor of a waiting message closed with what was typed,
  // and its close (rider Q) let the message start with its old words. A change drops the turns; the waiting messages,
  // the researcher's own words, stay until the record is read again.
  async aChangeElsewhereLeavesAnOpenWaitingEditorAndItsText(w, strict) {
    const state = start({ personal: { ui: { follow_up: 'queue' } }, records: { a: { turns: [], queued: [] } } });
    const s = await windowWorld(w, strict, state);
    await s.type('First question'); await s.send();
    const first = s.server.next(writes('POST', '/message/stream')).open();
    state.records.a = { queued: [], turns: [{ run_id: 'r1', status: 'running', message: { text: 'First question' }, answer: null }] };
    await s.act(() => first.push({ type: 'run_started', run_id: 'r1', conversation_id: 'a', seq: 1 }));
    await s.type('Waiting message'); await s.send();
    state.records.a.queued = [{ id: 'q1', text: 'Waiting message' }];
    await s.release(s.server.next(writes('POST', '/queue')), (r) => r.answer({ id: 'q1', text: 'Waiting message' }));
    await s.press('Edit');
    assert.equal(await s.typeInto(byId('queued-q1'), NEWER, 'the edit'), true);
    state.records.a.turns[0] = { ...state.records.a.turns[0], status: 'succeeded', answer: { text: 'An answer' }, result_saved: true };
    await s.act(() => { first.push({ type: 'run_finished', status: 'succeeded' }); first.end(); }); // held by its editor (rider Q)
    s.server.hold('conversation');
    await s.act(() => w.library.libraryChanged('p1')); // a change elsewhere: the record is read again
    assert.equal(s.has((e) => e.props.value === NEWER), true, 'the editor closed while the record is read again');
    await s.releaseAll(reads('conversation'));
    assert.equal(s.has((e) => e.props.value === NEWER), true, 'the editor closed once the record was read again');
    assert.equal(s.server.asked.filter(writes('POST', '/queue/q1/send')).length, 0, 'it started with its old words');
  },

  // The batch's internal review (F2): the form K fields and the Subagents add, which only the census held. Each: the
  // text sent, then something newer typed while its request is under way, then the answer: the newer text stays.
  async aProviderKeySavedKeepsAKeyTypedSince(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [
      { name: 'lab', kind: 'openai-compatible', base_url: 'https://llm.example.org/v1', enabled: true, has_key: false }] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    await s.typeInto(byId('key-lab'), SUBMITTED, 'the key');
    await s.submitWith(byId('key-lab'), 'the key form');
    const put = s.server.next(writes('PUT', '/api/keys/lab'));
    assert.equal(await s.typeInto(byId('key-lab'), NEWER, 'the key'), true);
    state.gets['/api/providers'].providers[0].has_key = true; // the reread after the save moves the card to Ready (AF)
    await s.release(put, (r) => r.answer({}));
    assert.equal(s.field(byId('key-lab'), 'the key').props.value, NEWER);
  },
  async anAllowlistAddKeepsAModelTypedSince(w, strict) {
    const routes = { routes: [], checked_on: '2026-01-01' };
    const s = await partWorld(w, strict, { gets: { '/api/private-routes': routes } }, 'Governance', 'PrivateAllowlist');
    await s.typeInto(byId('allowlist-add'), 'model/submitted', 'the model');
    await s.submitWith(byId('allowlist-add'), 'the add form');
    const put = s.server.next(writes('PUT', '/api/private-routes/'));
    assert.equal(await s.typeInto(byId('allowlist-add'), NEWER, 'the model'), true);
    await s.release(put, (r) => r.answer(routes));
    assert.equal(s.field(byId('allowlist-add'), 'the model').props.value, NEWER);
  },
  async aRefusedBackupKeepsAPassphraseTypedSince(w, strict) {
    const s = await partWorld(w, strict, { gets: { '/api/backups': { backups: [] } } }, 'Backups', 'BackupsSection');
    const folder = (e) => e.type === 'ui:Input' && e.props.type !== 'password' && e.up.some((x) => x.type === 'form' && x.text.includes('Full backup'));
    await s.typeInto(folder, '/backups', 'the folder');
    await s.typeInto(byId('full-backup-passphrase'), SUBMITTED, 'the passphrase');
    await s.submitWith(byId('full-backup-passphrase'), 'the full backup form');
    const start = s.server.next(writes('POST', '/api/backups/full'));
    assert.equal(await s.typeInto(byId('full-backup-passphrase'), NEWER, 'the passphrase'), true);
    await s.release(start, (r) => r.fail('destination_invalid', 400));
    assert.equal(s.field(byId('full-backup-passphrase'), 'the passphrase').props.value, NEWER);
  },
  async aRefusedExportKeepsAPassphraseTypedSince(w, strict) {
    const s = await partWorld(w, strict, {}, 'ProjectExport', 'ProjectExportSection', { project: { id: 'p1', sensitivity: 'private' } });
    await s.typeInto((e) => e.type === 'ui:Input' && e.props.type !== 'password', '/exports', 'the folder');
    await s.typeInto(byId('export-passphrase'), SUBMITTED, 'the passphrase');
    await s.submitWith(byId('export-passphrase'), 'the export form');
    const start = s.server.next(writes('POST', '/api/projects/p1/export'));
    assert.equal(await s.typeInto(byId('export-passphrase'), NEWER, 'the passphrase'), true);
    await s.release(start, (r) => r.fail('destination_invalid', 400));
    assert.equal(s.field(byId('export-passphrase'), 'the passphrase').props.value, NEWER);
  },
  async aSubagentAddedWhileAChangeIsSavedKeepsWhatWasTyped(w, strict) {
    const s = await settingsWorld(w, strict, { personal: { ui: { language: 'en' }, subagents: { models: [] } } }, { open: false, startPage: null });
    await s.redraw({ open: true, startPage: 'subagents' });
    await s.typeInto(byId('subagent-add'), 'model/submitted', 'a model to add');
    await s.submitWith(byId('subagent-add'), 'the add form');
    assert.equal(s.server.asked.filter((r) => r.method !== 'GET').length, 1, 'the first add is saved');
    await s.typeInto(byId('subagent-add'), NEWER, 'a model to add');
    await s.submitWith(byId('subagent-add'), 'the add form'); // while the first is saved: nothing is added
    assert.equal(s.server.asked.filter((r) => r.method !== 'GET').length, 1, 'a second save was asked');
    assert.equal(s.field(byId('subagent-add'), 'a model to add').props.value, NEWER, 'what was typed was cleared');
  },
  // Found on the way (not this class; new with design S's "not decided" window): a project is deleted while its Library
  // panel is open. The Library is drawn for no project, and its effect throws (library.js takeOpening reads a null).
  async aProjectDeletedWithItsLibraryOpenLeavesTheWindowDrawn(w, strict) {
    const state = start({ projects: [P1, { id: 'p2', name: 'Project Beta', kind: 'research' }], papers: [paper('a', 'Paper A')] });
    const s = await windowWorld(w, strict, state);
    await s.press('Library');
    await s.projectMenu(/^Delete Project Alpha/);
    await s.press('Delete');
    const del = s.server.next(writes('DELETE', '/api/projects/p1'));
    state.projects = [state.projects[1]];
    s.server.hold('projects');
    await s.release(del, (r) => r.answer({ ok: true })); // throws here at the head: the Library's effect
    await s.releaseAll(reads('projects'));
    assert.ok(s.screen().includes('Project Beta'), 'another project is open');
  },
  // AF's note beside T, the same state: a file dropped on a Library drawn for no project was added to project.id of
  // undefined, a TypeError in the drop's handler. Nothing is added, and nothing throws.
  async aFileDroppedOnALibraryForNoProjectAddsNothing(w, strict) {
    const s = await partWorld(w, strict, {}, 'Library', 'Library', { project: undefined });
    const rejected = []; const note = (error) => rejected.push(String(error));
    process.on('unhandledRejection', note);
    try {
      await s.act(() => one(s.shown().filter((e) => e.props.onDrop), 'the drop zone').props.onDrop({ preventDefault() {}, stopPropagation() {},
        dataTransfer: { files: [{ name: 'a.pdf', type: 'application/pdf', size: 1, data: '' }] } }));
    } finally {
      process.off('unhandledRejection', note);
    }
    assert.deepEqual(rejected, [], 'the drop threw');
    assert.equal(s.server.asked.filter((r) => r.method !== 'GET').length, 0, 'something was sent');
  },
  // This review, a credential: a passphrase typed for a Private project's export is sent, and encrypts the export, after
  // the project became Normal and its field went.
  async aPassphraseNoLongerShownIsNotSent(w, strict) {
    const s = await drawn(w, strict, {}, 'ProjectExport', 'ProjectExportSection', { project: { id: 'p1', sensitivity: 'private' } });
    await s.typeInto((e) => e.type === 'ui:Input' && e.props.type !== 'password', '/exports', 'the folder');
    await s.typeInto(byId('export-passphrase'), SUBMITTED, 'the passphrase');
    await s.redraw({ project: { id: 'p1', sensitivity: 'normal' } });
    assert.ok(!s.has(byId('export-passphrase')), 'a Normal project is asked for no passphrase');
    await s.submitWith((e) => e.type === 'ui:Input', 'the export form');
    assert.equal(s.server.next(writes('POST', '/api/projects/p1/export')).body.passphrase, undefined, 'the passphrase no longer shown was sent');
  },
  // r8 pass 1, site 1: a provider's first key is saved; a replacement is typed while the save is under way; the reread
  // after the save moves the card from Needs setup to Ready.
  async aFirstKeySavedKeepsAReplacementTypedSince(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [lab()] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    await s.typeInto(byId('key-lab'), SUBMITTED, 'the key');
    await s.submitWith(byId('key-lab'), 'the key form');
    const put = s.server.next(writes('PUT', '/api/keys/lab'));
    assert.equal(put.body.key, SUBMITTED);
    assert.equal(await s.typeInto(byId('key-lab'), NEWER, 'the key'), true);
    state.gets['/api/providers'].providers[0].has_key = true; // as the reread after the save says
    await s.release(put, (r) => r.answer({ ok: true }));
    assert.ok(s.shown().some((e) => e.type === 'h3' && e.text.trim() === 'Ready'), 'the card is under Ready');
    assert.equal(s.field(byId('key-lab'), 'the key').props.value, NEWER, 'the replacement typed since is gone');
    assert.equal(s.server.asked.filter(writes('PUT', '/api/keys')).length, 0, 'the replacement was sent');
  },
  // This review, the same remount: what the save said (the key store was unavailable: the key is in a file) is the
  // card's too, and goes with it.
  async aFirstKeysStoreWarningIsStillShown(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [lab()] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    await s.typeInto(byId('key-lab'), SUBMITTED, 'the key');
    await s.submitWith(byId('key-lab'), 'the key form');
    state.gets['/api/providers'].providers[0].has_key = true;
    await s.release(s.server.next(writes('PUT', '/api/keys/lab')), (r) => r.answer({ ok: true, warning: 'credential_store_unavailable' }));
    assert.ok(s.says('status').some((text) => text.includes(KEY_STORE)), 'the key store warning is not shown');
  },
  // This review, the same remount by another answer: a key typed and not saved, and the provider turned off (or on).
  async aKeyTypedIsKeptWhenItsProviderIsTurnedOff(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [lab({ has_key: true })] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    assert.equal(await s.typeInto(byId('key-lab'), NEWER, 'the key'), true);
    await s.press('Off');
    state.gets['/api/providers'].providers[0].enabled = false;
    await s.release(s.server.next(writes('PUT', '/api/settings')), (r) => r.answer({ values: { providers: { lab: { enabled: false } } }, hash: 'h2', problems: [] }));
    assert.ok(s.shown().some((e) => e.type === 'h3' && e.text.trim() === 'Off'), 'the card is under Off');
    assert.equal(s.field(byId('key-lab'), 'the key').props.value, NEWER, 'the key typed is gone');
  },
  // Guard (green at the head): a card whose group changes is drawn under its new group, once.
  async aProviderIsListedOnceUnderItsGroup(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [lab(), lab({ name: 'other', has_key: true })] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    const order = () => s.shown().filter((e) => e.type === 'h3' || e.type === 'h4').map((e) => e.text.trim());
    assert.deepEqual(order(), ['Ready', 'other', 'Needs setup', 'lab']);
    await s.typeInto(byId('key-lab'), SUBMITTED, 'the key');
    await s.submitWith(byId('key-lab'), 'the key form');
    state.gets['/api/providers'].providers[0].has_key = true;
    await s.release(s.server.next(writes('PUT', '/api/keys/lab')), (r) => r.answer({ ok: true }));
    assert.deepEqual(order(), ['Ready', 'lab', 'other']);
    assert.equal(s.field(byId('key-lab'), 'the key').props.value, '', 'the key saved is cleared');
  },
  // r8 pass 1, site 2: Add endpoint is submitted; while its key is stored the form is cancelled, opened again and typed
  // in; the first form's answer closes the second.
  async anEarlierAddEndpointDoesNotCloseAReopenedForm(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    await s.press('Add an endpoint');
    await s.typeInto(byId('new-provider-name'), 'first', 'name');
    await s.typeInto(byId('new-provider-url'), 'http://localhost:1/v1', 'address');
    await s.typeInto(byId('new-provider-key'), SUBMITTED, 'key');
    await s.submitWith(byId('new-provider-name'), 'the add form');
    await s.release(s.server.next(writes('PUT', '/api/settings')), (r) => r.answer({ values: { providers: { first: {} } }, hash: 'h2', problems: [] }));
    const put = s.server.next(writes('PUT', '/api/keys/first'));
    const cancelled = await pressIfCan(s, 'Cancel');
    let typed = false;
    if (cancelled) {
      await s.press('Add an endpoint');
      typed = await s.typeInto(byId('new-provider-name'), 'second', 'name');
      await s.typeInto(byId('new-provider-key'), NEWER, 'key');
    }
    state.gets['/api/providers'].providers.push(lab({ name: 'first', has_key: true }));
    await s.release(put, (r) => r.answer({ ok: true }));
    assert.ok(s.screen().includes('first'), 'the endpoint added is listed');
    if (typed) {
      assert.ok(s.has((e) => e.props.value === 'second'), 'the name typed in the form opened since is gone');
      assert.ok(s.has((e) => e.props.value === NEWER), 'the key typed in the form opened since is gone');
    }
  },
  // This review, a credential: Cancel pressed while the endpoint's settings are written; its key is stored all the same.
  async aCancelledAddEndpointStoresNoKey(w, strict) {
    const state = { personal: { providers: {} }, gets: { '/api/providers': { providers: [] } } };
    const s = await partWorld(w, strict, state, 'Providers', 'Providers');
    await s.press('Add an endpoint');
    await s.typeInto(byId('new-provider-name'), 'first', 'name');
    await s.typeInto(byId('new-provider-url'), 'http://localhost:1/v1', 'address');
    await s.typeInto(byId('new-provider-key'), SUBMITTED, 'key');
    await s.submitWith(byId('new-provider-name'), 'the add form');
    const write = s.server.next(writes('PUT', '/api/settings'));
    const cancelled = await pressIfCan(s, 'Cancel');
    await s.release(write, (r) => r.answer({ values: { providers: { first: {} } }, hash: 'h2', problems: [] }));
    const stored = s.server.asked.filter(writes('PUT', '/api/keys/first')).length;
    assert.ok(!cancelled || stored === 0, 'Cancel was pressed, and the key was stored after it');
  },
  // r8 pass 1, site 3: an import is submitted; its form is closed and opened again and another path typed; the first
  // import's answer closes the second form.
  async anEarlierImportDoesNotCloseAReopenedForm(w, strict) {
    const state = { gets: { '/api/helper': helper(false) } };
    const s = await partWorld(w, strict, state, 'LocalHelper', 'LocalHelperSection', { project: null });
    await s.press('Import a file…');
    await s.typeInto(inForm, '/first/model.gguf', 'the path');
    await s.submitWith(inForm, 'the import form');
    const post = s.server.next(writes('POST', '/api/helper/models/import'));
    let typed;
    if (await pressIfCan(s, 'Import a file…')) { // closed
      await s.press('Import a file…'); // and opened again
      typed = await s.typeInto(inForm, NEWER, 'the path');
    } else typed = await s.typeInto(inForm, NEWER, 'the path');
    state.gets['/api/helper'] = helper(true);
    await s.release(post, (r) => r.answer(helper(true)));
    assert.ok(!typed || s.has((e) => e.props.value === NEWER), 'the path typed in the form opened since is gone');
  },
  // The same, where it is the status read that says the model is in place (the first import's answer still held): no
  // callback of the first form runs.
  async aModelInPlaceDoesNotCloseAFormOpenedSinceItsImport(w, strict) {
    const state = { gets: { '/api/helper': helper(false) } };
    const timers = [];
    const s = await drawn(w, strict, state, 'LocalHelper', 'LocalHelperSection', { project: null },
      () => { globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; }; });
    await s.press('Import a file…');
    await s.typeInto(inForm, '/first/model.gguf', 'the path');
    await s.submitWith(inForm, 'the import form');
    s.server.next(writes('POST', '/api/helper/models/import')); // held: never answered here
    let typed;
    if (await pressIfCan(s, 'Import a file…')) { await s.press('Import a file…'); typed = await s.typeInto(inForm, NEWER, 'the path'); }
    else typed = await s.typeInto(inForm, NEWER, 'the path');
    state.gets['/api/helper'] = helper(true);
    await s.act(() => timers.at(-1)()); // the status is read again
    assert.ok(!s.has(inForm), 'the import is still offered though the model is in place');
    assert.equal(typed, false, 'a path was typed in a form opened while an import was under way, and went when the model was in place');
  },
  // r8's note: Choose stays enabled while the path is read-only, and writes it.
  async aPathChosenWhileItsImportIsUnderWayChangesNothing(w, strict) {
    const state = { gets: { '/api/helper': helper(false) } };
    const s = await drawn(w, strict, state, 'LocalHelper', 'LocalHelperSection', { project: null },
      () => { globalThis.window.pywebview = { api: { choose_model_file: async () => '/chosen/model.gguf' } }; });
    await s.press('Import a file…');
    await s.typeInto(inForm, '/first/model.gguf', 'the path');
    await s.submitWith(inForm, 'the import form');
    const chose = await pressIfCan(s, 'Choose…');
    assert.equal(chose, false, 'Choose could be pressed while the import was under way');
    assert.equal(s.field(inForm, 'the path').props.value, '/first/model.gguf');
  },
  // This review (the census lists the folder field once, for every form that draws it): the data-folder screen's form is
  // replaced by its request's answer, over a path typed since.
  async aDataFolderChosenLosesNothingTypedSince(w, strict) {
    const s = await partWorld(w, strict, {}, 'DataFolderScreen', 'DataFolderScreen', { health: { data_folder: '/old', data_folder_problem: 'missing' } });
    await s.typeInto(inForm, '/first', 'the folder');
    await s.submitWith(inForm, 'the folder form');
    const post = s.server.next(writes('POST', '/api/data-folder'));
    const typed = await s.typeInto(inForm, NEWER, 'the folder');
    await s.release(post, (r) => r.answer({ data_folder: '/first' }));
    assert.ok(s.screen().includes('/first'), 'the folder recorded is shown');
    assert.equal(typed, false, 'another folder could be typed while the first was recorded, and the form then went');
  },
  // This review (the census lists CommitField once, for every form that draws it): the picker's window field is closed
  // by its own save's answer, over a number typed since.
  async aWindowSetInThePickerLosesNothingTypedSince(w, strict) {
    const state = { personal: {}, gets: {
      '/api/providers': { providers: [lab({ has_key: true })] }, '/api/models/recent': { models: [] },
      '/api/providers/lab/models': { status: {}, models: [{ id: 'm1', name: 'Model One', offered: true, window: { status: 'needed' }, effort: { steps: [] } }] } } };
    const s = await partWorld(w, strict, state, 'ModelPicker', 'ModelPicker', { projectId: null });
    await s.act(() => one(s.shown().filter((e) => e.type === 'button' && e.props.role === 'option' && e.text.includes('Model One')), 'the model').props.onClick());
    assert.equal(await s.typeInto(byId('picker-window'), '8192', 'the window'), true);
    await s.act(() => s.field(byId('picker-window'), 'the window').props.onKeyDown({ key: 'Enter', preventDefault() {} }));
    const put = s.server.next(writes('PUT', '/api/settings'));
    const typed = await s.typeInto(byId('picker-window'), '16384', 'the window');
    await s.release(put, (r) => r.answer({ values: {}, hash: 'h2' }));
    assert.ok(!s.has(byId('picker-window')), 'the window field is closed once its window is set');
    assert.equal(typed, false, 'another window could be typed while the first was saved, and the field then closed');
  },
  // Part 2a's mutation runs found these four parts of rule C held by no scenario above; each is pinned here.
  // While an import is under way its form cannot be closed ("Import a file..." is greyed, as Harold decided).
  async anImportUnderWayCannotBeClosed(w, strict) {
    const s = await partWorld(w, strict, { gets: { '/api/helper': helper(false) } }, 'LocalHelper', 'LocalHelperSection', { project: null });
    await s.press('Import a file…');
    await s.typeInto(inForm, '/first/model.gguf', 'the path');
    await s.submitWith(inForm, 'the import form');
    assert.equal(await pressIfCan(s, 'Import a file…'), false, 'the import form could be closed while its import was under way');
  },
  // A failed import's line is the form's: closing and opening the form again clears it, as a new form's was.
  async aFailedImportsLineGoesWhenItsFormIsOpenedAgain(w, strict) {
    const s = await partWorld(w, strict, { gets: { '/api/helper': helper(false) } }, 'LocalHelper', 'LocalHelperSection', { project: null });
    await s.press('Import a file…');
    await s.typeInto(inForm, '/first/model.gguf', 'the path');
    await s.submitWith(inForm, 'the import form');
    await s.release(s.server.next(writes('POST', '/api/helper/models/import')), (r) => r.fail('file_mismatch', 409));
    assert.equal(s.says('alert').length, 1, 'the failure is said');
    await s.press('Import a file…'); await s.press('Import a file…');
    assert.deepEqual(s.says('alert'), [], 'an earlier import\'s failure is said in the form opened since');
  },
  // The data-folder screen's Choose is greyed while its folder is recorded (FolderField's readOnly), as the field is.
  async aDataFolderBeingRecordedCannotBeChosenAgain(w, strict) {
    const s = await drawn(w, strict, {}, 'DataFolderScreen', 'DataFolderScreen', { health: { data_folder: '/old', data_folder_problem: 'missing' } },
      () => { globalThis.window.pywebview = { api: { choose_folder: async () => '/chosen' } }; });
    await s.typeInto(inForm, '/first', 'the folder');
    await s.submitWith(inForm, 'the folder form');
    assert.equal(await pressIfCan(s, 'Choose…'), false, 'Choose could be pressed while the folder was recorded');
  },
  // Enter pressed again in the picker's window field while its window is saved sends nothing more.
  async aWindowBeingSavedIsNotSentAgain(w, strict) {
    const state = { personal: {}, gets: {
      '/api/providers': { providers: [lab({ has_key: true })] }, '/api/models/recent': { models: [] },
      '/api/providers/lab/models': { status: {}, models: [{ id: 'm1', name: 'Model One', offered: true, window: { status: 'needed' }, effort: { steps: [] } }] } } };
    const s = await partWorld(w, strict, state, 'ModelPicker', 'ModelPicker', { projectId: null });
    await s.act(() => one(s.shown().filter((e) => e.type === 'button' && e.props.role === 'option' && e.text.includes('Model One')), 'the model').props.onClick());
    await s.typeInto(byId('picker-window'), '8192', 'the window');
    const enter = () => s.act(() => s.field(byId('picker-window'), 'the window').props.onKeyDown({ key: 'Enter', preventDefault() {} }));
    await enter(); await enter();
    await s.release(s.server.next(writes('PUT', '/api/settings')), (r) => r.answer({ values: {}, hash: 'h2' }));
    const windows = s.server.asked.filter((r) => writes('PUT', '/api/settings')(r) && Object.keys(r.body.updates).some((k) => k.includes('windows')));
    assert.equal(windows.length, 0, 'the window was saved again after it was saved'); // saves wait their turn (api.js saveSettings)
  },
  // The batch I review (P3): the import's action is the section's, so a form that went when the status said the model
  // was in place, and comes back when a later status says it is needed again, said the failure of an import made in
  // the form before. The form now closes when the model is in place; opening it again clears that line.
  async aFormThatComesBackSaysNoEarlierImportsFailure(w, strict) {
    const state = { gets: { '/api/helper': helper(false) } };
    const timers = [];
    const s = await drawn(w, strict, state, 'LocalHelper', 'LocalHelperSection', { project: null },
      () => { globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; }; });
    await s.press('Import a file…');
    await s.typeInto(inForm, '/first/model.gguf', 'the path');
    await s.submitWith(inForm, 'the import form');
    const post = s.server.next(writes('POST', '/api/helper/models/import'));
    state.gets['/api/helper'] = helper(true);
    await s.act(() => timers.at(-1)()); // the status is read again: the model is in place
    await s.release(post, (r) => r.fail('file_mismatch', 409)); // the import made in the form fails after it went
    state.gets['/api/helper'] = helper(false);
    await s.act(() => timers.at(-1)()); // and a later status says the model is needed again
    assert.deepEqual(s.says('alert'), [], 'an import made in a form that went is said to have failed');
  },
  // The batch I review (P2): a window whose save fails leaves its field to take typing again, with the reason.
  async aWindowWhoseSaveFailedCanBeTypedAgain(w, strict) {
    const state = { personal: {}, gets: {
      '/api/providers': { providers: [lab({ has_key: true })] }, '/api/models/recent': { models: [] },
      '/api/providers/lab/models': { status: {}, models: [{ id: 'm1', name: 'Model One', offered: true, window: { status: 'needed' }, effort: { steps: [] } }] } } };
    const s = await partWorld(w, strict, state, 'ModelPicker', 'ModelPicker', { projectId: null });
    await s.act(() => one(s.shown().filter((e) => e.type === 'button' && e.props.role === 'option' && e.text.includes('Model One')), 'the model').props.onClick());
    await s.typeInto(byId('picker-window'), '8192', 'the window');
    await s.act(() => s.field(byId('picker-window'), 'the window').props.onKeyDown({ key: 'Enter', preventDefault() {} }));
    await s.release(s.server.next(writes('PUT', '/api/settings')), (r) => r.fail('internal'));
    assert.equal(s.says('alert').length, 1, 'the failure is said');
    assert.equal(await s.typeInto(byId('picker-window'), '16384', 'the window'), true, 'the field takes nothing after its save failed');
  },
  // PR 63's recheck r9 (pass 1 finding 2, pass 2 finding 3): while a model's window is saved, the picker's search and
  // its choices are busy too (AF's rule C for the whole picker): a late success clears no search typed since, and a
  // failure shows no draft or error under a model chosen since.
  async aWindowSavedInThePickerClearsNoSearchTypedSince(w, strict) {
    const s = await partWorld(w, strict, picking(), 'ModelPicker', 'ModelPicker', { projectId: null });
    await windowFor(s, 'Model One', '8192');
    const put = s.server.next(writes('PUT', '/api/settings'));
    const typed = await s.typeInto(byId('picker-search'), NEWER, 'the search');
    await s.release(put, (r) => r.answer({ values: {}, hash: 'h2' }));
    assert.ok(!typed || s.field(byId('picker-search'), 'the search').props.value === NEWER, 'a search typed while the window was saved is gone');
  },
  async aWindowSavedInThePickerTakesNoOtherChoiceMeanwhile(w, strict) {
    const s = await partWorld(w, strict, picking(), 'ModelPicker', 'ModelPicker', { projectId: null });
    await windowFor(s, 'Model One', '8192');
    const put = s.server.next(writes('PUT', '/api/settings'));
    const two = option(s, 'Model Two');
    const chose = !two.props.disabled;
    if (chose) await s.act(() => two.props.onClick());
    await s.release(put, (r) => r.fail('internal'));
    if (chose) {
      assert.ok(!s.screen().includes('Model Two') || !s.has((e) => e.props.value === '8192'), 'Model One\'s draft is shown under Model Two');
      assert.equal(s.says('alert').length, 0, 'Model One\'s failure is said under Model Two');
    }
    assert.equal(chose, false, 'another model could be chosen while a window was saved');
  },
  async aWindowSavedInThePickerKeepsThePickerOpenUntilItsAnswer(w, strict) {
    const s = await partWorld(w, strict, picking(), 'ModelPicker', 'ModelPicker', { projectId: null });
    await windowFor(s, 'Model One', '8192');
    const popover = one(s.elements().filter((e) => e.type === 'ui:Popover'), 'the picker');
    await s.act(() => popover.props.onOpenChange(false)); // Escape, or a press outside it
    assert.equal(one(s.elements().filter((e) => e.type === 'ui:Popover'), 'the picker').props.open, true, 'the picker closed while its window was saved');
  },
};

// Design review AF's row vi: when a removal becomes known the projects are read again, and Settings' This project page
// and the Library are drawn anew, with what was typed there. Kept as todo: they run, and are red by design.
const limits = {
  // This review, row vi: the projects are read again after a removal became known; the open project's record is not
  // known meanwhile, and Settings' This project page is drawn again, with what was typed there. Drawn as the Shell draws
  // Settings: its project prop undefined, then the record again.
  async aProjectsRereadKeepsWhatIsTypedInThisProject(w, strict) {
    const project = { id: 'p1', name: 'Project Alpha', kind: 'research', sensitivity: 'normal' };
    const state = { personal: { limits: { agent_steps: 1, tool_calls: 1, turn_minutes: 1 } }, gets: {
      '/api/settings?project_id=p1': { values: { project: {}, limits: {} }, hash: 'h', problems: [] },
      '/api/instructions?project_id=p1': { text: '', hash: 'i', other_bytes: 0, cap_bytes: 32768 } } };
    const s = await settingsWorld(w, strict, state, { open: false, startPage: null, project });
    await s.redraw({ open: true, startPage: 'project' });
    assert.equal(await s.typeInto(byId('project-instructions'), NEWER, 'the instructions'), true);
    await s.redraw({ open: true, startPage: 'project', project: undefined }); // the list is read again
    await s.redraw({ open: true, startPage: 'project' });
    assert.equal(s.field(byId('project-instructions'), 'the instructions').props.value, NEWER, 'the instructions typed are gone');
  },
  // This review, row vi as the window draws it: a project's deletion fails (it may have happened); the projects are read
  // again, the open project's record is not known meanwhile, and its Library is drawn anew: what was typed in its search
  // field goes.
  async aFailedDeletionKeepsTheLibrarysSearchText(w, strict) {
    const s = await windowWorld(w, strict, start({ papers: [paper('a', 'Paper A')] }));
    await s.press('Library');
    const search = (e) => e.type === 'ui:Input' && e.up.some((x) => x.props.role === 'search');
    assert.equal(await s.typeInto(search, DRAFT, 'the search field'), true);
    await s.projectMenu(/^Delete Project Alpha/);
    await s.press('Delete');
    const del = s.server.next(writes('DELETE', '/api/projects/p1'));
    s.server.hold('projects');
    await s.release(del, (r) => r.fail());
    await s.releaseAll(reads('projects'));
    assert.ok(s.screen().includes('Paper A'), 'the project is still open, its papers read again');
    assert.equal(s.field(search, 'the search field').props.value, DRAFT, 'what was typed in the search field is gone');
  },
};

const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
const LIMIT = 'accepted limit row vi (Harold, 2026-11-22)';
for (const [name, run] of [...Object.entries(scenarios), ...Object.entries(limits)]) {
  if (only && !only.includes(name)) continue;
  for (const strict of [false, true]) {
    test(`${name}${strict ? ' (StrictMode)' : ''}`, name in limits ? { todo: LIMIT } : {}, async () => {
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
