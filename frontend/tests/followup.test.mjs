// S1-33 (F11): the budget bar's figures, a message sent while a turn runs (as the one setting says, never a choice in
// the message box), and which queued message a finished turn starts.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { budgetBar, follow, nextQueued } from '../src/followup.js';
import { drain, send, stop, subscribe } from '../src/live.js';

const spend = (conversation, project, percents = [50, 85, 90]) => ({
  conversation: { limit_usd: 10, settled_usd: conversation, open_usd: 0, own: false },
  project: { limit_usd: 50, settled_usd: project, open_usd: 0 }, notice_percents: percents });

test('the bar appears at 50% of whichever budget is closer to its cap, marking 85% and 90%', () => {
  assert.equal(budgetBar(spend(4.9, 1)), null); // 49%
  for (const [settled, percent] of [[5, 50], [8.5, 85], [9, 90], [12, 120]]) {
    const bar = budgetBar(spend(settled, 1));
    assert.deepEqual([bar.budget, bar.percent, bar.marks], ['conversation', percent, [85, 90]]);
  }
  const project = budgetBar(spend(1, 45)); // 10% of the conversation's, 90% of the project's
  assert.deepEqual([project.budget, project.settled, project.limit], ['project', 45, 50]);
  assert.equal(budgetBar({ ...spend(0, 0), conversation: null }), null);
  assert.deepEqual(budgetBar(spend(6, 0, [60, 80])).marks, [80]); // the setting's own percents
  assert.equal(budgetBar(spend(5.5, 0, [60, 80])), null);
});

test('a message sent while a turn runs follows the setting: steered at its run, or queued', async (t) => {
  const sent = [];
  let mode = 'steer';
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    sent.push([init?.method ?? 'GET', String(url), init?.body ? JSON.parse(init.body) : null]);
    if (String(url).endsWith('/api/settings')) return Response.json({ values: { ui: { follow_up: mode } } });
    return Response.json(String(url).includes('/steering') ? { steering: { run_id: 'r1', seq: 4 } } : { id: 'q1', text: 'x' });
  });
  assert.deepEqual(await follow('c1', 'r1', 'Only 2020'), { steering: { run_id: 'r1', seq: 4 } });
  assert.deepEqual(sent.at(-1).slice(0, 3), ['POST', '/api/runs/r1/steering', { content: 'Only 2020' }]);
  mode = 'queue';
  assert.deepEqual(await follow('c1', 'r1', 'Next'), { queued: { id: 'q1', text: 'x' } });
  assert.deepEqual(sent.at(-1).slice(0, 3), ['POST', '/api/conversations/c1/queue', { content: 'Next' }]);
  mode = 'steer';
  await follow('c1', null, 'Before it is admitted'); // no run to steer yet: it waits as the next turn
  assert.equal(sent.at(-1)[1], '/api/conversations/c1/queue');
});

test('a finished turn starts the oldest queued message only when it succeeded and is still the latest', () => {
  const read = (status, runId = 'r1') => ({ turns: [{ run_id: 'r0', status: 'succeeded' }, { run_id: runId, status }],
    queued: [{ id: 'q1' }, { id: 'q2' }] });
  assert.equal(nextQueued(read('succeeded'), 'r1').id, 'q1');
  for (const status of ['cancelled', 'failed', 'interrupted', 'running']) assert.equal(nextQueued(read(status), 'r1'), null);
  assert.equal(nextQueued(read('succeeded', 'r2'), 'r1'), null); // another turn started since
  assert.equal(nextQueued({ ...read('succeeded'), queued: [] }, 'r1'), null);
});

test('the message box offers no steer or queue choice, and 409 active_run follows the setting instead', () => {
  const source = readFileSync(fileURLToPath(new URL('../src/components/ConversationView.jsx', import.meta.url)), 'utf8');
  assert.ok(!/followUp(Steer|Queue)/.test(source)); // the setting's own labels appear only in Settings
  assert.match(source, /if \(isActiveRun\(error\)\) return followUp\(text\)/);
  assert.match(source, /if \(running\) return followUp\(text\)/);
});

test('no budget text promises a strict ceiling (F11, What a budget promises; SPEC:1223)', () => {
  const catalog = JSON.parse(readFileSync(fileURLToPath(new URL('../src/i18n/en.json', import.meta.url)), 'utf8'));
  const budgetText = Object.entries(catalog).filter(([key]) => /budget/i.test(key)).map(([, value]) => JSON.stringify(value));
  assert.ok(budgetText.length > 10);
  for (const text of budgetText) assert.ok(!/ceiling|cannot exceed|never exceed|guarantee/i.test(text), text);
});

test('the waiting messages and the bar are outside the message box\'s form, so their forms never nest in it', () => {
  const source = readFileSync(fileURLToPath(new URL('../src/components/ConversationView.jsx', import.meta.url)), 'utf8');
  const composer = source.slice(source.indexOf('function Composer('), source.indexOf('const MENTION'));
  assert.ok(composer.length > 0 && !/<Queued|<BudgetBar/.test(composer));
});

function streamOf(...chunks) {
  const encoder = new TextEncoder();
  return new ReadableStream({ start(controller) { for (const c of chunks) controller.enqueue(encoder.encode(c)); controller.close(); } });
}

// Review recheck 1: a turn that succeeded starts the oldest waiting message on its own route, once; another ending
// starts nothing.
for (const [status, started] of [['succeeded', true], ['failed', false], ['cancelled', false]]) {
  test(`after a turn that ended ${status}, ${started ? 'the oldest waiting message starts once' : 'nothing starts'}`, async (t) => {
    const posts = [];
    t.mock.method(globalThis, 'fetch', async (url, init) => {
      url = String(url);
      if (url === '/x') {
        return new Response(streamOf(`data: {"type":"run_started","run_id":"r1-${status}"}\n\n`,
          `data: {"type":"run_finished","status":"${status}"}\n\n`));
      }
      if (init?.method === 'POST') {
        posts.push([url, JSON.parse(init.body)]);
        return new Response(streamOf('data: {"type":"run_started","run_id":"r2"}\n\n', 'data: {"type":"run_finished","status":"succeeded"}\n\n'));
      }
      return Response.json({ turns: [{ run_id: `r1-${status}`, status }], queued: [{ id: 'q1', text: 'Next' }, { id: 'q2', text: 'Later' }] });
    });
    await send(`c-${status}`, '/x', { model: 'm1', provider: 'openrouter' }, 'Q');
    await new Promise((resolve) => setTimeout(resolve, 30));
    const started_ = posts.filter(([url]) => url.includes('/queue/'));
    assert.deepEqual(started_, started ? [[`/api/conversations/c-${status}/queue/q1/send`,
      { model: 'm1', provider: 'openrouter', after: `r1-${status}` }]] : []);
  });
}

// PR 52 review 1 (finding 4): a drain whose read was asked before a manual send started never starts its message, and
// Stop still cancels the manual turn. (PR 52 cycle 3: the drain reads the record itself, so the turn before is one sent
// here, whose own end asked for the read.)
function openStream(runId) { // a stream that has started its run and stays open until close()
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; c.enqueue(new TextEncoder().encode(`data: {"type":"run_started","run_id":"${runId}"}\n\n`)); } });
  return { body, close: () => controller.close() };
}

test('a drain whose read was asked before a manual send starts nothing, and Stop still cancels the manual turn', async (t) => {
  const posts = [];
  let answerRead;
  const manual = openStream('r-manual');
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    if (init?.method === 'POST') posts.push(url);
    if (url === '/api/conversations/c-race') return new Promise((resolve) => { answerRead = resolve; });
    if (url === '/first') return new Response(streamOf('data: {"type":"run_started","run_id":"r1"}\n\n', 'data: {"type":"run_finished","status":"succeeded"}\n\n'));
    if (url === '/manual') return new Response(manual.body);
    return Response.json({ run_id: 'r-manual', status: 'cancelled' });
  });
  await send('c-race', '/first', {}, 'First'); // it succeeds, and its end reads the conversation
  while (!answerRead) await new Promise((resolve) => setTimeout(resolve, 2));
  await send('c-race', '/manual', {}, 'Typed now'); // admitted: its run started
  answerRead(Response.json({ turns: [{ run_id: 'r1', status: 'succeeded' }], queued: [{ id: 'q1', text: 'Next' }] }));
  await settle();
  assert.deepEqual(posts.filter((url) => url.includes('/queue/')), []);
  await stop('c-race');
  assert.ok(posts.includes('/api/runs/r-manual/cancel'));
  manual.close();
});

// Fix batch 1 (F1): an admitted turn whose first event has not been read yet keeps the conversation: its events show and
// Stop reaches it. (PR 52 cycle 3: a message sent meanwhile is refused here, with no request.)
function lateStream() { // an admitted turn whose events have not arrived
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  const encoder = new TextEncoder();
  return { body, event: (text) => controller.enqueue(encoder.encode(`data: ${text}\n\n`)), close: () => controller.close() };
}

test('a message sent while an admitted turn\'s run_started is unread is refused here; the turn shows, and Stop reaches it', async (t) => {
  const posts = [];
  const requested = [];
  const admitted = lateStream();
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    requested.push(url);
    if (init?.method === 'POST' && url.includes('/cancel')) posts.push(url);
    if (url === '/admitted') return new Response(admitted.body);
    return Response.json({});
  });
  const seen = [];
  const unsubscribe = subscribe((id, turn, kind) => { if (id === 'c-five') seen.push([kind, turn?.runId ?? null]); });
  send('c-five', '/admitted', {}, 'Admitted');
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal((await send('c-five', '/refused', {}, 'Refused').catch((error) => error)).code, 'active_run');
  assert.ok(!requested.includes('/refused'));
  admitted.event('{"type":"run_started","run_id":"r-five"}');
  await new Promise((resolve) => setTimeout(resolve, 5));
  unsubscribe();
  assert.deepEqual(seen.at(-1), ['run_started', 'r-five']);
  await stop('c-five');
  assert.deepEqual(posts, ['/api/runs/r-five/cancel']);
  admitted.close();
});

test('Stop reaches an admitted turn by closing its stream when its run_started is still unread', async (t) => {
  let signal;
  const admitted = lateStream();
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    if (url === '/admitted2') { signal = init.signal; return new Response(admitted.body); }
    return Response.json({ code: 'active_run' }, { status: 409 });
  });
  send('c-six', '/admitted2', {}, 'Admitted');
  await new Promise((resolve) => setTimeout(resolve, 5));
  await send('c-six', '/refused2', {}, 'Refused').catch((error) => error);
  await stop('c-six');
  assert.equal(signal.aborted, true);
  admitted.close();
});

// S1-33 fix batch 3 (PR 52 cycle 3, design review B): one stream at a time per conversation here, a drain that reads the
// record each time, and a Stop mark here. Each case is one the reviews found (C1a, C1b, C2, C3, B-R2, N1) or one of
// design review B's probes.
function controlledStream() { // a stream whose events and end the test sends, each when it chooses
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  const encoder = new TextEncoder();
  return { body, event: (text) => controller.enqueue(encoder.encode(`data: ${text}\n\n`)), close: () => controller.close() };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
const abortError = () => Object.assign(new Error('aborted'), { name: 'AbortError' });
const succeeded = (runId) => new Response(streamOf(`data: {"type":"run_started","run_id":"${runId}"}\n\n`,
  'data: {"type":"run_finished","status":"succeeded"}\n\n'));

// A fetch for one conversation: `streams` by path (each a function of the request's init), the conversation's reads
// (`read`, a function of their count), each start of a waiting message answered by the next of `starts` (by default a
// turn r-next that succeeds), and cancels; `calls` records what was asked.
function backend({ read, starts = [], streams = {} }) {
  const calls = { requests: [], reads: 0, queueSends: [], cancels: [] };
  const fetch = async (url, init) => {
    url = String(url);
    calls.requests.push(url);
    if (streams[url]) return streams[url](init);
    if (url.includes('/cancel')) { calls.cancels.push(url); return Response.json({ status: 'cancelled' }); }
    if (url.includes('/queue/')) {
      calls.queueSends.push([url, JSON.parse(init.body)]);
      return (starts.shift() ?? (() => succeeded('r-next')))(init);
    }
    calls.reads += 1;
    return read(calls.reads);
  };
  return { calls, fetch };
}
const record = (runId, status, queued = [{ id: 'q1', text: 'Next' }]) => Response.json({ turns: [{ run_id: runId, status }], queued });

test('a message sent while a turn is admitted or runs here is refused here with no request, and one start follows its success (C2)', async (t) => {
  const a = controlledStream();
  const run = backend({ streams: { '/a': () => new Response(a.body) }, read: () => record('r-c2', 'succeeded') });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-c2', '/a', { model: 'm1' }, 'A');
  assert.equal((await send('c-c2', '/b', {}, 'B').catch((error) => error)).code, 'active_run'); // being admitted
  a.event('{"type":"run_started","run_id":"r-c2"}');
  await sending;
  assert.equal((await send('c-c2', '/b', {}, 'B').catch((error) => error)).code, 'active_run'); // running
  assert.ok(!run.calls.requests.includes('/b')); // never sent, so nothing is ever handed back
  a.event('{"type":"run_finished","status":"succeeded"}');
  a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-c2/queue/q1/send', { model: 'm1', after: 'r-c2' }]]);
});

test('Stop reaches the one turn here, admitted or still unread, and a send still pending; nothing waiting starts after it', async (t) => {
  for (const phase of ['pending', 'unread', 'running']) {
    const a = controlledStream();
    const signals = {};
    const conversation = `c-stop-${phase}`;
    const run = backend({ read: () => record(conversation, 'succeeded'), streams: { '/a': (init) => {
      signals.a = init.signal;
      if (phase === 'pending') return new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(abortError())));
      return new Response(a.body);
    } } });
    t.mock.method(globalThis, 'fetch', run.fetch);
    const sending = send(conversation, '/a', {}, 'A').catch(() => {});
    if (phase === 'running') a.event(`{"type":"run_started","run_id":"${conversation}"}`);
    await settle();
    assert.equal((await send(conversation, '/b', {}, 'B').catch((error) => error)).code, 'active_run', phase);
    await stop(conversation);
    if (phase === 'running') {
      assert.deepEqual(run.calls.cancels, [`/api/runs/${conversation}/cancel`]);
      a.event('{"type":"run_finished","status":"succeeded"}'); // its answer had committed before the Stop
    } else {
      assert.equal(signals.a.aborted, true, phase); // closing its stream stops a turn admitted or about to be
    }
    if (phase !== 'pending') a.close();
    await sending;
    await settle();
    assert.deepEqual(run.calls.queueSends, [], phase);
    t.mock.restoreAll();
  }
});

test('after Stop, a drain whose read was still out starts nothing more (C1a)', async (t) => {
  // Two reads of the conversation are out; the first starts the waiting message; Stop closes that start before it is
  // admitted; then the second read comes back: nothing more is sent.
  const answers = [];
  const run = backend({ streams: { '/a': () => succeeded('r-c1a') }, read: () => new Promise((resolve) => answers.push(resolve)),
    starts: [(init) => new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(abortError())))] });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-c1a', '/a', { model: 'm1' }, 'A');
  await settle(); // its end's drain reads the conversation
  // a second drain, as a follow-up queued meanwhile asks for one; drain(conversation) reads the record itself, and the
  // run id and route given here are what it took before PR 52 cycle 3, so this test runs against that code too
  drain('c-c1a', 'r-c1a', { model: 'm1' });
  await settle();
  assert.equal(answers.length, 2);
  answers[0](record('r-c1a', 'succeeded'));
  await settle();
  assert.equal(run.calls.queueSends.length, 1);
  await stop('c-c1a');
  answers[1](record('r-c1a', 'succeeded'));
  await settle();
  assert.equal(run.calls.queueSends.length, 1);
});

test('a turn whose success arrives after Stop starts nothing waiting (C1b)', async (t) => {
  const a = controlledStream();
  const run = backend({ streams: { '/a': () => new Response(a.body) }, read: () => record('r-c1b', 'succeeded') });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-c1b', '/a', {}, 'A');
  a.event('{"type":"run_started","run_id":"r-c1b"}');
  await sending;
  await stop('c-c1b');
  a.event('{"type":"run_finished","status":"succeeded"}'); // its answer committed before the Stop reached it
  a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
});

test('Stop after a success: nothing starts, nor after a refused message; after a message the researcher sends is admitted, the waiting one follows it', async (t) => {
  const a = controlledStream();
  let latest = 'r-s1';
  const run = backend({ read: () => record(latest, 'succeeded'), streams: {
    '/a': () => new Response(a.body),
    '/refused': () => Response.json({ code: 'no_provider' }, { status: 400 }),
    '/b': () => { latest = 'r-s2'; return succeeded('r-s2'); } } });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-s', '/a', { model: 'm1' }, 'A');
  a.event('{"type":"run_started","run_id":"r-s1"}');
  await sending;
  a.event('{"type":"run_finished","status":"succeeded"}');
  await stop('c-s'); // after its last event, before its stream's end
  a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
  assert.equal((await send('c-s', '/refused', {}, 'Refused').catch((error) => error)).code, 'no_provider');
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
  await send('c-s', '/b', { model: 'm2' }, 'B');
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-s/queue/q1/send', { model: 'm2', after: 'r-s2' }]]);
});

test('a follow-up queued while the turn\'s first event is unread starts once that turn succeeds, from the record (C3)', async (t) => {
  const a = controlledStream();
  const run = backend({ streams: { '/a': () => new Response(a.body) }, read: () => record('r-c3', 'succeeded') });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-c3', '/a', { model: 'm1' }, 'A');
  // the follow-up's queue reply came back: a turn is being admitted here, so it waits (null and {}: what the view
  // passed before PR 52 cycle 3, its run id still unread)
  drain('c-c3', null, {});
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
  a.event('{"type":"run_started","run_id":"r-c3"}');
  await sending;
  a.event('{"type":"run_finished","status":"succeeded"}');
  a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends.map(([url]) => url), ['/api/conversations/c-c3/queue/q1/send']);
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.match(followUp, /else if \(sent\.queued\) drain\(id\)/); // no turns or run id the handler captured
});

for (const change of ['edited', 'removed']) {
  test(`a waiting message ${change} while it was being started gives way to the next, read again (B-R2)`, async (t) => {
    const run = backend({ streams: { '/a': () => succeeded(`r-br-${change}`) },
      read: (count) => record(`r-br-${change}`, 'succeeded', count === 1 ? [{ id: 'q1', text: 'Next' }, { id: 'q2', text: 'Later' }]
        : change === 'edited' ? [{ id: 'q1', text: 'Edited' }, { id: 'q2', text: 'Later' }] : [{ id: 'q2', text: 'Later' }]),
      starts: [() => Response.json({ code: 'not_queued' }, { status: 409 })] });
    t.mock.method(globalThis, 'fetch', run.fetch);
    await send(`c-br-${change}`, '/a', {}, 'A');
    await settle();
    assert.deepEqual(run.calls.queueSends.map(([url]) => url.split('/').at(-2)), change === 'edited' ? ['q1', 'q1'] : ['q1', 'q2']);
  });
}

test('a message sent after the turn\'s last event but before its stream\'s end is refused here, and starts once, in either order', async (t) => {
  for (const order of ['queued first', 'end first']) {
    const a = controlledStream();
    const conversation = `c-last-${order.split(' ')[0]}`;
    let queued = [];
    const run = backend({ streams: { '/a': () => new Response(a.body) }, read: () => record(conversation, 'succeeded', queued) });
    t.mock.method(globalThis, 'fetch', run.fetch);
    const sending = send(conversation, '/a', { model: 'm1' }, 'A');
    a.event(`{"type":"run_started","run_id":"${conversation}"}`);
    await sending;
    a.event('{"type":"run_finished","status":"succeeded"}');
    assert.equal((await send(conversation, '/b', {}, 'B').catch((error) => error)).code, 'active_run', order);
    if (order === 'end first') { a.close(); await settle(); } // its end reads no waiting message yet
    queued = [{ id: 'q1', text: 'B' }]; // the view queues it (the setting), then asks for a drain
    drain(conversation);
    if (order === 'queued first') a.close();
    await settle();
    assert.deepEqual(run.calls.queueSends.map(([url]) => url), [`/api/conversations/${conversation}/queue/q1/send`], order);
    t.mock.restoreAll();
  }
});

test('a steer answered "ended" after Stop waits with Send; one answered so with no Stop since starts once nothing runs here (N1)', async (t) => {
  const { idle, stopsAt } = await import('../src/live.js');
  const a = controlledStream();
  t.mock.method(globalThis, 'fetch', async (url) => (String(url) === '/a' ? new Response(a.body) : Response.json({ status: 'cancelled' })));
  const sending = send('c-n1', '/a', {}, 'A');
  a.event('{"type":"run_started","run_id":"r-n1"}');
  await sending;
  const pressed = stopsAt('c-n1');
  let free = false;
  idle('c-n1').then(() => { free = true; });
  await stop('c-n1');
  assert.equal(stopsAt('c-n1'), pressed + 1); // the view compares it with what it read at the submit
  await settle();
  assert.equal(free, false); // its stream is still under way here
  a.event('{"type":"run_finished","status":"cancelled"}');
  a.close();
  await settle();
  assert.equal(free, true);
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.match(followUp, /const pressed = stopsAt\(id\)/);
  assert.match(followUp, /await idle\(id\);[^\n]*\n\s*if \(stopsAt\(id\) === pressed && !halt\(id\)\) await sendQueued\(sent\.queued\)/);
  // item 4 as Harold worded it (internal review P3-2): a Stop pressed before the submit counts too, until a turn the
  // researcher starts there is admitted, so steer and queue alike leave the message waiting with Send
  const { halt } = await import('../src/live.js');
  assert.equal(halt('c-n1'), true);
  t.mock.method(globalThis, 'fetch', async () => new Response(streamOf('data: {"type":"run_started","run_id":"r-n1b"}\n\n',
    'data: {"type":"run_finished","status":"failed"}\n\n')));
  await send('c-n1', '/b', {}, 'B');
  assert.equal(halt('c-n1'), false);
});

test('Stop with a send pending here also stops the turn the record shows running from another window', async (t) => {
  const cancels = [];
  let signal;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    if (url.includes('/cancel')) { cancels.push(url); return Response.json({}); }
    signal = init.signal;
    return new Promise((resolve, reject) => init.signal.addEventListener('abort',
      () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))));
  });
  const pending = send('c-d14c', '/pending', {}, 'Here').catch((error) => error);
  await settle();
  await stop('c-d14c', 'r-elsewhere');
  assert.deepEqual(cancels, ['/api/runs/r-elsewhere/cancel']);
  assert.equal(signal.aborted, true);
  await pending;
});

test('a send still pending keeps the conversation when another message is sent meanwhile: that one is refused here, and Stop reaches the first', async (t) => {
  const cancels = [];
  const requested = [];
  const older = controlledStream();
  let admit;
  t.mock.method(globalThis, 'fetch', async (url) => {
    url = String(url);
    requested.push(url);
    if (url.includes('/cancel')) { cancels.push(url); return Response.json({}); }
    if (url === '/older') return new Promise((resolve) => { admit = () => resolve(new Response(older.body)); });
    return Response.json({ code: 'active_run' }, { status: 409 });
  });
  const seen = [];
  const unsubscribe = subscribe((id, turn) => { if (id === 'c-pend') seen.push(turn?.runId ?? null); });
  send('c-pend', '/older', {}, 'Older').catch(() => {});
  await settle();
  assert.equal((await send('c-pend', '/newer', {}, 'Newer').catch((error) => error)).code, 'active_run');
  assert.ok(!requested.includes('/newer'));
  admit();
  await settle();
  older.event('{"type":"run_started","run_id":"r-older"}');
  await settle();
  unsubscribe();
  assert.equal(seen.at(-1), 'r-older');
  await stop('c-pend');
  assert.deepEqual(cancels, ['/api/runs/r-older/cancel']);
  older.close();
});

test('a queued stream event marks the live turn, so the view reads the waiting messages again (D11)', async () => {
  const { apply } = await import('../src/live.js');
  assert.equal(apply(apply({ text: 'Q' }, { type: 'queued' }), { type: 'queued' }).queued, 2);
  const source = readFileSync(fileURLToPath(new URL('../src/components/ConversationView.jsx', import.meta.url)), 'utf8');
  assert.match(source, /if \(live\?\.steered \|\| live\?\.queued\) load\(\)/);
});

// Fix batch 2 (PR 52 cycle 2): the follow-up's setting, the amount a budget needs, a waiting message's route, small
// amounts, the bar's edges, and a steer whose turn had ended.
const VIEW = () => readFileSync(fileURLToPath(new URL('../src/components/ConversationView.jsx', import.meta.url)), 'utf8');

test('a failed read of the follow-up setting sends nothing: neither steer nor queue is chosen for the researcher (D12)', async (t) => {
  const posts = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    if (init?.method === 'POST') posts.push(String(url));
    if (String(url).endsWith('/api/settings')) return Response.json({ code: 'internal' }, { status: 500 });
    return Response.json({});
  });
  await assert.rejects(follow('c1', 'r1', 'Only 2020'), (error) => error.code === 'internal');
  assert.deepEqual(posts, []);
});

test('a limit too low for the step says the least amount it needs, rounded up so typing it fits (D13)', async (t) => {
  const { api } = await import('../src/api.js');
  const { problemText } = await import('../src/action.js');
  const { makeT } = await import('../src/i18n/index.js');
  const zh = JSON.parse(readFileSync(fileURLToPath(new URL('../src/i18n/zh-CN.json', import.meta.url)), 'utf8'));
  t.mock.method(globalThis, 'fetch', async () => Response.json({ code: 'limit_too_low', message: 'x', needed_usd: 1.2301 },
    { status: 400 }));
  const error = await api('POST', '/api/runs/r/asks/a', { text: '1' }).catch((e) => e);
  assert.equal(error.neededUsd, 1.2301);
  assert.match(problemText(makeT('en'), error, 'en'), /\$1\.24/);
  assert.match(problemText(makeT('zh-CN', { 'zh-CN': zh }), error, 'zh-CN'), /US\$1\.24/);
  assert.equal(problemText(makeT('en'), Object.assign(new Error('x'), { code: 'x' }), 'en'), makeT('en')('errors.internal'));
});

test('a waiting message starts on the route its turn was sent on, never the picker\'s choice now; after a turn sent elsewhere, nothing starts here (D15)', async (t) => {
  let latest = 'r-d15';
  const run = backend({ streams: { '/x': () => succeeded('r-d15') }, read: () => record(latest, 'succeeded') });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-d15', '/x', { content: 'Q', model: 'm-a', provider: 'openrouter', effort: 'high', material_ids: ['p'] }, 'Q');
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-d15/queue/q1/send',
    { model: 'm-a', provider: 'openrouter', effort: 'high', after: 'r-d15' }]]);
  latest = 'r-elsewhere'; // the latest turn was sent from another window: that one starts it, or it waits with Send
  await drain('c-d15');
  assert.equal(run.calls.queueSends.length, 1);
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.ok(!followUp.includes('currentChoice'), followUp);
});

test('a positive amount too small for four decimals is never shown as $0 (D17)', async () => {
  const { money } = await import('../src/text.js');
  const { auditDetail } = await import('../src/audit.js');
  const { makeT } = await import('../src/i18n/index.js');
  assert.equal(money(0.00001, 'en'), '$0.00001');
  assert.equal(money(0.0000123, 'zh-CN'), 'US$0.000012');
  assert.equal(money(0.0042, 'en'), '$0.0042'); // unchanged above it
  assert.equal(money(0, 'en'), '$0.00'); // nothing spent, or reported as zero
  const details = auditDetail(makeT('en'), { event: 'budget_decision', data: { to_usd: 0.00001 } }, new Intl.DateTimeFormat('en'));
  assert.ok(!/\$0\.00(?![0-9])/.test(JSON.stringify(details)) && /0\.00001/.test(JSON.stringify(details)), JSON.stringify(details));
});

test('the bar\'s edges: no notice percents, no bar; its meter never above its maximum; read again after a budget answer (D19)', () => {
  assert.equal(budgetBar(spend(9, 0, [])), null);
  assert.equal(budgetBar(spend(12, 0)).meter, 100);
  assert.equal(budgetBar(spend(5.04, 0)).meter, 50);
  const bar = readFileSync(fileURLToPath(new URL('../src/components/FollowUp.jsx', import.meta.url)), 'utf8');
  assert.match(bar, /aria-valuenow=\{bar\.meter\}/);
  assert.match(VIEW(), /if \(done\?\.budget\) load\(\)/);
});

test('a steer whose turn had ended starts as a new turn, as Send does, once nothing runs here (D22, reading 12)', () => {
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.match(followUp, /if \(sent\.ended\) \{[^}]*await idle\(id\);[^\n]*\n\s*if \(stopsAt\(id\) === pressed && !halt\(id\)\) await sendQueued\(sent\.queued\)/);
});

test('a budget ask whose project budget could not be read says so, never that the budget is $0 (E4), in both catalogs', () => {
  const ask = readFileSync(fileURLToPath(new URL('../src/components/Ask.jsx', import.meta.url)), 'utf8');
  assert.match(ask, /budget\?\.unread \? t\('ask\.budget\.unread'\)/);
  for (const language of ['en', 'zh-CN']) {
    const catalog = JSON.parse(readFileSync(fileURLToPath(new URL(`../src/i18n/${language}.json`, import.meta.url)), 'utf8'));
    assert.ok(catalog['ask.budget.unread'] && !/\{limit\}/.test(catalog['ask.budget.unread']), language);
  }
});

test('a message naming papers sent while a turn runs waits as the next turn with them, whatever the setting, and says so (item 5, SPEC:281)', async (t) => {
  const sent = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    sent.push([init?.method ?? 'GET', String(url), init?.body ? JSON.parse(init.body) : null]);
    if (String(url).endsWith('/api/settings')) return Response.json({ values: { ui: { follow_up: 'steer' } } });
    return Response.json({ id: 'q1', text: 'x', material_ids: ['p1'] });
  });
  assert.deepEqual(await follow('c1', 'r1', 'Only this one', ['p1']), { queued: { id: 'q1', text: 'x', material_ids: ['p1'] } });
  assert.deepEqual(sent, [['POST', '/api/conversations/c1/queue', { content: 'Only this one', material_ids: ['p1'] }]]);
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.match(followUp, /follow\(id, runId, text, papers\.map\(\(paper\) => paper\.id\)\)/);
  assert.match(followUp, /if \(papers\.length\) choosePapers\(projectId, \[\]\)/);
  const composer = VIEW().slice(VIEW().indexOf('function Composer('), VIEW().indexOf('const MENTION'));
  assert.match(composer, /chosen\.length > 0 && running && [^\n]*\n\s*<p[^>]*>\{t\('composer\.papersWait'\)\}/);
  // Send of one whose paper was deleted since says so in words for a waiting message, never "Choose again"
  assert.match(VIEW(), /error\.code === 'selection_invalid'\) \{[^}]*setProblem\(t\('followUp\.paperGone'\)\)/);
  for (const language of ['en', 'zh-CN']) {
    const catalog = JSON.parse(readFileSync(fileURLToPath(new URL(`../src/i18n/${language}.json`, import.meta.url)), 'utf8'));
    assert.ok(catalog['composer.papersWait'] && catalog['followUp.paperGone'], language);
  }
});
