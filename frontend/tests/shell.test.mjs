import test from 'node:test';
import assert from 'node:assert/strict';
import { makeT } from '../src/i18n/index.js';
import { DEFAULTS, LIMITS, columns, fromSettings, panelShareAt, toSettings } from '../src/layout.js';
import { imageAsLink, safeHref } from '../src/links.js';
import { SESSION_KEY, takeSession } from '../src/session.js';
import { EventReader } from '../src/sse.js';
import { continuable, moveTargets, projectName } from '../src/projects.js';
import { errorText, money } from '../src/text.js';
import { apply, send, subscribe } from '../src/live.js';
import { packageRoot } from '../licenses.mjs';

const open = (width, extra = {}) => columns({ width, ...DEFAULTS, panelOpen: false, ...extra });

test('three columns at their defaults, and the limits hold', () => {
  assert.deepEqual(open(1440), { narrow: false, sidebarShown: true, sidebar: 248, panel: 0, overlay: false });
  const panel = open(1440, { panelOpen: true });
  assert.equal(panel.panel, Math.round((1440 - 248) / 2));
  assert.equal(open(1440, { sidebarWidth: 10 }).sidebar, LIMITS.sidebarMin);
  assert.equal(open(1440, { sidebarWidth: 900 }).sidebar, LIMITS.sidebarMax);
  const wide = open(1440, { panelOpen: true, panelShare: 0.95 });
  assert.equal(1440 - 248 - wide.panel, LIMITS.conversationMin); // the conversation keeps its minimum
  assert.equal(open(1440, { panelOpen: true, panelShare: 0.01 }).panel, LIMITS.panelMin);
});

test('under 1,000 pixels the sidebar is a drawer, and a panel that does not fit slides over', () => {
  assert.equal(open(999).narrow, true);
  assert.equal(open(999).sidebar, 0);
  assert.equal(open(900, { panelOpen: true }).overlay, false); // 360 + 380 fit in 900
  const small = open(700, { panelOpen: true });
  assert.equal(small.overlay, true);
  assert.ok(small.panel >= LIMITS.panelMin && small.panel <= 700 - 48);
  assert.equal(open(1200, { sidebarOpen: false }).sidebarShown, false);
});

test('a dragged panel divider gives a share within the limits', () => {
  assert.equal(panelShareAt(1440 - 596, { width: 1440, sidebar: 248 }), 0.5);
  const far = panelShareAt(300, { width: 1440, sidebar: 248 });
  assert.equal(Math.round(far * (1440 - 248)), 1440 - 248 - LIMITS.conversationMin);
});

test('the layout round-trips through [ui.layout], with defaults for what is missing or wrong', () => {
  assert.deepEqual(fromSettings({}), DEFAULTS);
  assert.deepEqual(fromSettings({ ui: { layout: { sidebar_width: '300', sidebar_open: 'no', panel_share: null } } }), DEFAULTS);
  const layout = { sidebarWidth: 300.4, sidebarOpen: false, panelShare: 0.61234 };
  const saved = toSettings(layout);
  assert.deepEqual(saved, { 'ui.layout.sidebar_width': 300, 'ui.layout.sidebar_open': false, 'ui.layout.panel_share': 0.612 });
  assert.deepEqual(fromSettings({ ui: { layout: { sidebar_width: 300, sidebar_open: false, panel_share: 0.612 } } }),
    { sidebarWidth: 300, sidebarOpen: false, panelShare: 0.612 });
});

test('model output links only to the web and mail, and its images become links', () => {
  assert.equal(safeHref(' https://example.org/a '), 'https://example.org/a');
  assert.equal(safeHref('mailto:a@example.org'), 'mailto:a@example.org');
  for (const bad of ['javascript:alert(1)', 'data:text/html,x', '/local', 'file:///etc/passwd', null]) {
    assert.equal(safeHref(bad), null, bad);
  }
  assert.deepEqual(imageAsLink('https://example.org/f.png', ' Figure 1 '), { href: 'https://example.org/f.png', label: 'Figure 1' });
  assert.deepEqual(imageAsLink('data:image/png;base64,AAAA', ''), { href: null, label: null });
});

function memoryStorage() {
  const items = new Map();
  return { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, v) };
}

test('the session is taken from the fragment, kept for reloads, and removed from the address', () => {
  const storage = memoryStorage();
  const replaced = [];
  const history = { replaceState: (...args) => replaced.push(args[2]) };
  const secret = 'a'.repeat(43);
  assert.equal(takeSession({ hash: `#session=${secret}`, pathname: '/', search: '' }, storage, history), secret);
  assert.deepEqual(replaced, ['/']);
  assert.equal(storage.getItem(SESSION_KEY), secret);
  assert.equal(takeSession({ hash: '', pathname: '/', search: '' }, storage, history), secret); // a reload
  assert.equal(takeSession({ hash: '#session=short', pathname: '/', search: '' }, memoryStorage(), history), null);
});

test('server-sent events are read across chunks, and a malformed one is skipped', () => {
  const reader = new EventReader();
  assert.deepEqual(reader.push('data: {"type":"run_star'), []);
  assert.deepEqual(reader.push('ted","run_id":"r"}\r\n\r\ndata: {broken}\n\ndata: {"type":"step"}\n\n'),
    [{ type: 'run_started', run_id: 'r' }, { type: 'step' }]);
});

test('projects are named in the interface language, and moves go only to as strict or stricter', () => {
  const t = makeT('zh-CN');
  assert.equal(projectName(t, { kind: 'general', name: 'General' }), '通用');
  assert.equal(projectName(t, { kind: 'research', name: 'Wages' }), 'Wages');
  const projects = [{ id: 'g', sensitivity: 'normal' }, { id: 'p', sensitivity: 'private' }, { id: 'l', sensitivity: 'local_only' }];
  assert.deepEqual(moveTargets(projects, projects[1]).map((p) => p.id), ['l']);
  assert.deepEqual(moveTargets(projects, projects[0]).map((p) => p.id), ['p', 'l']);
});

test('only an interrupted turn, or one stopped at a limit or by a project change, continues', () => {
  assert.equal(continuable({ status: 'interrupted' }), true);
  assert.equal(continuable({ status: 'cancelled', cancel_reason: 'limit' }), true);
  assert.equal(continuable({ status: 'cancelled', cancel_reason: 'revoked' }), true);
  assert.equal(continuable({ status: 'cancelled', cancel_reason: 'researcher' }), false);
  assert.equal(continuable({ status: 'failed' }), false);
});

test('errors show their own text, or the general one; costs never show as zero', () => {
  const t = makeT('en');
  assert.equal(errorText(t, 'active_run'), t('errors.active_run'));
  assert.equal(errorText(t, 'no_such_code'), t('errors.internal'));
  assert.equal(money(0.0042, 'en'), '$0.0042');
  assert.equal(money(1.5, 'en'), '$1.50');
  assert.equal(money(undefined, 'en'), null);
});

test('a live turn follows its events', () => {
  let turn = { text: 'Q', runId: null, answer: null, done: false };
  turn = apply(turn, { type: 'run_started', run_id: 'r1' });
  turn = apply(turn, { type: 'chat_response', content: 'A', result_saved: false });
  turn = apply(turn, { type: 'run_finished', status: 'failed' });
  assert.deepEqual(turn, { text: 'Q', runId: 'r1', answer: 'A', resultSaved: false, done: true });
  assert.equal(apply(turn, { type: 'limit_reached' }).limit, true);
});

function streamOf(...chunks) {
  const encoder = new TextEncoder();
  return new ReadableStream({ start(controller) { for (const c of chunks) controller.enqueue(encoder.encode(c)); controller.close(); } });
}

test('send resolves when the turn is admitted, and rejects with the refusal when it is not', async (t) => {
  const seen = [];
  const unsubscribe = subscribe((id, turn, type) => seen.push([id, type, turn?.done ?? null]));
  t.after(unsubscribe);
  t.mock.method(globalThis, 'fetch', async () => new Response(streamOf(
    'data: {"type":"run_started","run_id":"r1"}\n\n', 'data: {"type":"run_finished","status":"succeeded"}\n\n')));
  await send('c1', '/api/conversations/c1/message/stream', { content: 'Q' }, 'Q');
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(seen, [['c1', 'send', false], ['c1', 'run_started', false], ['c1', 'run_finished', true]]);

  t.mock.method(globalThis, 'fetch', async () => Response.json({ code: 'no_provider' }, { status: 400 }));
  await assert.rejects(send('c2', '/x', {}, 'Q'), (error) => error.code === 'no_provider');
  assert.deepEqual(seen.at(-1), ['c2', 'refused', null]);
});

test('the license plugin finds the package a module belongs to', () => {
  assert.deepEqual(packageRoot('/a/node_modules/react/index.js'), { name: 'react', root: '/a/node_modules/react' });
  assert.deepEqual(packageRoot('\0/a/node_modules/x/node_modules/@radix-ui/react-id/dist/index.mjs?v=1'),
    { name: '@radix-ui/react-id', root: '/a/node_modules/x/node_modules/@radix-ui/react-id' });
  assert.equal(packageRoot('/a/src/App.jsx'), null);
});
