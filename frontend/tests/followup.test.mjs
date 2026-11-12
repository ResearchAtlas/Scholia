// S1-33 (F11): the budget bar's figures, a message sent while a turn runs (as the one setting says, never a choice in
// the message box), and which queued message a finished turn starts.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { budgetBar, follow, isActiveRun, nextQueued } from '../src/followup.js';
import { drain, send, stop, stopsAt, subscribe } from '../src/live.js';
import { ApiError, post } from '../src/api.js';

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
// starts nothing. (PR 52 cycle 4, G8: the start names the turn it follows only; the backend runs it on that turn's route.)
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
    assert.deepEqual(started_, started ? [[`/api/conversations/c-${status}/queue/q1/send`, { after: `r1-${status}` }]] : []);
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
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-c2/queue/q1/send', { after: 'r-c2' }]]);
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
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-s/queue/q1/send', { after: 'r-s2' }]]);
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
  assert.match(followUp, /\n\s*if \(sent\.queued\) drain\(id\);/); // no turns or run id the handler captured
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

// PR 52 cycle 4 (design review B2, Option 1 with variant 1N): the view's own handlers, taken from its source and run
// against the real live.js and followup.js, with what they close over in the component stood in for. Each case is one
// cycle 4 or design review B2 found.
function viewHandlers(id, overrides = {}) {
  const source = VIEW();
  const handler = (name) => { // its text, from `async function name(` to its matching brace
    const start = source.indexOf(`async function ${name}(`);
    let depth = 0;
    for (let i = source.indexOf('{', start); i < source.length; i += 1) {
      depth += source[i] === '{' ? 1 : source[i] === '}' ? -1 : 0;
      if (depth === 0) return source.slice(start, i + 1);
    }
    throw new Error(name);
  };
  const env = { id, projectId: 'p', conversation: { id }, here: { current: true }, running: false, live: null, shown: [],
    setProblem: (problem) => { if (problem) env.problems.push(problem); }, problems: [], readChoice: async () => {},
    currentChoice: () => null, messageRoute: () => ({}), t: (key) => key, errorText: (t, code) => code, load: async () => {},
    post, setDraft: () => {}, chosenPapers: () => [], choosePapers: () => {}, onCreated: () => {}, isActiveRun, ApiError,
    send, drain, stopsAt, follow, ...overrides };
  const names = Object.keys(env);
  const made = new Function(...names, `${['sendQueued', 'submit', 'followUp', 'resume'].map(handler).join('\n')}
    return { sendQueued, submit, followUp, resume };`)(...names.map((name) => env[name]));
  return { ...made, problems: env.problems };
}
const held = () => { let release; const promise = new Promise((resolve) => { release = resolve; }); return { promise, release }; };
const steering = (runId, reply) => ({ '/api/settings': () => Response.json({ values: { ui: { follow_up: 'steer' } } }),
  [`/api/runs/${runId}/steering`]: () => Response.json(reply) });

test('a late steer answered "ended" starts nothing on its own, and Send waiting on the saved choice sends nothing once Stop is pressed (B1/C1)', async (t) => {
  // cycle 4 B1 (C1): a fresh window, the record showing another window's turn running; the steer is answered "ended" as
  // that turn's cancellation settles, and Stop is pressed while the saved choice is still being read
  const choice = held();
  const run = backend({ read: () => record('r-b1', 'cancelled'),
    streams: steering('r-b1', { queued: { id: 'q1', text: 'Late' }, ended: true }) });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const view = viewHandlers('c-b1', { readChoice: () => choice.promise, running: true, shown: [{ run_id: 'r-b1', status: 'running' }] });
  const following = view.followUp('Late');
  await settle();
  await stop('c-b1');
  choice.release();
  await following;
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
  // Send pressed while the saved choice is read, then Stop: nothing is sent; its control, with no Stop, sends once
  for (const pressStop of [true, false]) {
    const conversation = `c-b1-send-${pressStop}`;
    const reading = held();
    const sends = backend({ read: () => record('r-old', 'cancelled'), starts: [() => succeeded(`${conversation}-r`)] });
    t.mock.method(globalThis, 'fetch', sends.fetch);
    const sending = viewHandlers(conversation, { readChoice: () => reading.promise }).sendQueued({ id: 'q1', text: 'Next' });
    await settle();
    if (pressStop) await stop(conversation);
    reading.release();
    await sending;
    await settle();
    assert.equal(sends.calls.queueSends.length, pressStop ? 0 : 1, conversation);
  }
});

test('a send refused after a success, while that turn\'s drain read is out, never loses the waiting message\'s start, in either order (C2)', async (t) => {
  for (const order of ['read first', 'refusal first']) {
    const conversation = `c-c2b-${order.split(' ')[0]}`;
    const first = held();
    let refuse;
    const run = backend({ read: (count) => (count === 1 ? first.promise : record(`${conversation}-a`, 'succeeded')), streams: {
      '/a': () => succeeded(`${conversation}-a`),
      '/b': () => new Promise((resolve) => { refuse = () => resolve(Response.json({ code: 'no_provider' }, { status: 400 })); }) } });
    t.mock.method(globalThis, 'fetch', run.fetch);
    await send(conversation, '/a', {}, 'A');
    await settle(); // its end's drain is reading the conversation
    const b = send(conversation, '/b', {}, 'B').catch((error) => error); // the researcher's own message, being admitted
    await settle();
    if (order === 'read first') { first.release(record(`${conversation}-a`, 'succeeded')); await settle(); refuse(); }
    else { refuse(); await settle(); first.release(record(`${conversation}-a`, 'succeeded')); }
    assert.equal((await b).code, 'no_provider');
    await settle();
    assert.deepEqual(run.calls.queueSends, [[`/api/conversations/${conversation}/queue/q1/send`, { after: `${conversation}-a` }]], order);
    t.mock.restoreAll();
  }
});

test('Send on a later message refused while the drain\'s read is out still lets the oldest start (B2 N1)', async (t) => {
  const first = held();
  const waiting = [{ id: 'q1', text: 'Oldest' }, { id: 'q2', text: 'Later' }];
  const run = backend({ read: (count) => (count === 1 ? first.promise : record('r-n1s', 'succeeded', waiting)),
    streams: { '/a': () => succeeded('r-n1s') },
    starts: [() => Response.json({ code: 'selection_invalid' }, { status: 400 }), () => succeeded('r-n1s-next')] });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-n1s', '/a', {}, 'A');
  await settle();
  const view = viewHandlers('c-n1s');
  const sending = view.sendQueued(waiting[1]); // a paper it names was deleted: refused
  await settle();
  first.release(record('r-n1s', 'succeeded', waiting));
  await sending;
  await settle();
  assert.deepEqual(view.problems, ['followUp.paperGone']);
  assert.deepEqual(run.calls.queueSends.map(([url, body]) => [url.split('/').at(-2), body]), [['q2', {}], ['q1', { after: 'r-n1s' }]]);
});

test('a late steer answered "ended" after a turn that succeeded here never starts before an older waiting message (B2 N2)', async (t) => {
  const a = controlledStream();
  const run = backend({ read: () => record('r-n2', 'succeeded', [{ id: 'q0', text: 'Older' }, { id: 'q1', text: 'Late' }]),
    streams: { '/a': () => new Response(a.body), ...steering('r-n2', { queued: { id: 'q1', text: 'Late' }, ended: true }) } });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-n2', '/a', {}, 'A');
  a.event('{"type":"run_started","run_id":"r-n2"}');
  await sending;
  const following = viewHandlers('c-n2', { running: true, live: { runId: 'r-n2', done: false } }).followUp('Late');
  await settle();
  a.event('{"type":"run_finished","status":"succeeded"}');
  a.close();
  await following;
  await settle();
  assert.deepEqual(run.calls.queueSends.map(([url, body]) => [url.split('/').at(-2), body]), [['q0', { after: 'r-n2' }]]);
});

test('a stream cut after its answer, with no end event, starts the waiting message once its record says it succeeded (B2 N3)', async (t) => {
  const run = backend({ read: () => record('r-n3', 'succeeded'), streams: { '/a': () => new Response(streamOf(
    'data: {"type":"run_started","run_id":"r-n3"}\n\n', 'data: {"type":"chat_response","content":"An answer.","result_saved":true}\n\n')) } });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-n3', '/a', {}, 'A');
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-n3/queue/q1/send', { after: 'r-n3' }]]);
});

test('a message, a Continue or a Send pressed here, then Stop before its request left, sends nothing; the message stays in the box (B2 N4)', async (t) => {
  for (const action of ['submit', 'resume', 'sendQueued']) {
    const conversation = `c-n4-${action}`;
    const reading = held();
    const run = backend({ read: () => record('r-n4', 'cancelled'), streams: {
      [`/api/conversations/${conversation}/message/stream`]: () => succeeded(`${conversation}-r`),
      '/api/runs/r-n4/continue': () => succeeded(`${conversation}-r`) } });
    t.mock.method(globalThis, 'fetch', run.fetch);
    const view = viewHandlers(conversation, { readChoice: () => reading.promise });
    const acting = action === 'submit' ? view.submit('Typed') : action === 'resume'
      ? view.resume({ run_id: 'r-n4', message: { text: 'Typed' } }) : view.sendQueued({ id: 'q1', text: 'Next' });
    await settle();
    await stop(conversation);
    reading.release();
    const done = await acting;
    await settle();
    assert.deepEqual(run.calls.requests.filter((url) => /message\/stream|continue|\/queue\//.test(url)), [], action);
    assert.deepEqual(view.problems, [], action); // nothing to say: the researcher stopped it
    if (action === 'submit') assert.equal(done, false); // so the text stays in the box
    t.mock.restoreAll();
  }
});

test('after a Stop in another window, a late steer answered "ended" starts nothing here (B2 N5)', async (t) => {
  // The turn was sent here and its answer committed; Stop was pressed in another window (the backend marks it stopped).
  const a = controlledStream();
  const started = [];
  const run = backend({ read: () => record('r-n5', 'succeeded'), streams: { '/a': () => new Response(a.body),
    ...steering('r-n5', { queued: { id: 'q1', text: 'Late' }, ended: true }) },
  starts: [1, 2, 3].map(() => (init) => { // as Harness.stopped makes the backend answer
    if (JSON.parse(init.body).after === 'r-n5') return Response.json({ code: 'not_after_success' }, { status: 409 });
    started.push(init.body);
    return succeeded('r-n5-next');
  }) });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-n5', '/a', {}, 'A');
  a.event('{"type":"run_started","run_id":"r-n5"}');
  await sending;
  const following = viewHandlers('c-n5', { running: true, live: { runId: 'r-n5', done: false } }).followUp('Late');
  await settle();
  a.event('{"type":"run_finished","status":"succeeded"}');
  a.close();
  await following;
  await settle();
  assert.deepEqual(started, []);
  assert.ok(run.calls.queueSends.every(([, body]) => body.after === 'r-n5'));
});

test('after Stop here, nothing waiting starts on its own until a turn the researcher starts here is admitted (N1, variant 1N)', async (t) => {
  const a = controlledStream();
  const run = backend({ read: () => record('r-n1', 'cancelled'),
    streams: { '/a': () => new Response(a.body), ...steering('r-n1', { queued: { id: 'q1', text: 'Late' }, ended: true }) } });
  t.mock.method(globalThis, 'fetch', run.fetch);
  const sending = send('c-n1', '/a', {}, 'A');
  a.event('{"type":"run_started","run_id":"r-n1"}');
  await sending;
  await stop('c-n1');
  await viewHandlers('c-n1', { running: true, live: { runId: 'r-n1', done: false } }).followUp('Late');
  a.event('{"type":"run_finished","status":"cancelled"}');
  a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
  // a start of its own is refused here while the conversation is halted, with no request
  assert.equal((await send('c-n1', '/auto', { after: 'r-n1' }, 'x', stopsAt('c-n1'), true).catch((error) => error)).code, 'stopped');
  assert.ok(!run.calls.requests.includes('/auto'));
});

// PR 52 cycle 5 (design review B3, Option 1, Harold 2026-11-12): the window remembers a start only once the backend
// answered it (admitted, or refused on its merits). Each case is one cycle 5 or design review B3 found.
for (const change of ['edited', 'removed']) {
  for (const order of ['read first', 'refusal first']) {
    test(`a start given up while the researcher's own message is pending is asked again once that one is refused: ${change}, ${order} (cycle 5 C1)`, async (t) => {
      // A succeeds; its drain starts q1, which is edited or removed meanwhile (not_queued); the retry's read is out when
      // the researcher's message B is sent; B is refused on its merits. In either order, the waiting message starts once.
      const conversation = `c-c5-${change}-${order.split(' ')[0]}`;
      const after = `${conversation}-a`;
      const retry = held();
      const left = change === 'edited' ? [{ id: 'q1', text: 'Edited' }, { id: 'q2', text: 'Later' }] : [{ id: 'q2', text: 'Later' }];
      let refuse;
      const run = backend({ read: (count) => (count === 1 ? record(after, 'succeeded', [{ id: 'q1', text: 'Next' }, { id: 'q2', text: 'Later' }])
        : count === 2 ? retry.promise : record(after, 'succeeded', left)),
      streams: { '/a': () => succeeded(after),
        '/b': () => new Promise((resolve) => { refuse = () => resolve(Response.json({ code: 'no_provider' }, { status: 400 })); }) },
      starts: [() => Response.json({ code: 'not_queued' }, { status: 409 })] });
      t.mock.method(globalThis, 'fetch', run.fetch);
      await send(conversation, '/a', {}, 'A');
      await settle(); // its drain's start was refused not_queued; the retry's read is out
      const b = send(conversation, '/b', {}, 'B').catch((error) => error);
      await settle();
      if (order === 'read first') { retry.release(record(after, 'succeeded', left)); await settle(); refuse(); }
      else { refuse(); await settle(); retry.release(record(after, 'succeeded', left)); }
      assert.equal((await b).code, 'no_provider');
      await settle();
      assert.deepEqual(run.calls.queueSends.map(([url, body]) => [url.split('/').at(-2), body]),
        [['q1', { after }], [change === 'edited' ? 'q1' : 'q2', { after }]]);
    });
  }
}

test('a start whose message was removed meanwhile, with none left to start, still starts a message queued later (design review B3, seed 91)', async (t) => {
  const run = backend({ read: (count) => record('r-s91', 'succeeded', count === 1 ? [{ id: 'q1', text: 'Next' }] : count === 2 ? []
    : [{ id: 'q3', text: 'Queued later' }]), streams: { '/a': () => succeeded('r-s91') },
  starts: [() => Response.json({ code: 'not_queued' }, { status: 409 })] });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-s91', '/a', {}, 'A');
  await settle(); // q1 removed as it was started; the retry reads none waiting
  assert.deepEqual(run.calls.queueSends.map(([url]) => url.split('/').at(-2)), ['q1']);
  drain('c-s91'); // a message joined the queue (the view's followUp asks for a drain)
  await settle();
  assert.deepEqual(run.calls.queueSends.map(([url, body]) => [url.split('/').at(-2), body]), [['q1', { after: 'r-s91' }],
    ['q3', { after: 'r-s91' }]]);
});

for (const [kind, answer, again] of [
  ['that is refused on its merits', () => Response.json({ code: 'not_after_success' }, { status: 409 }), 0],
  ['answered with a server failure', () => Response.json({ code: 'internal' }, { status: 500 }), 0],
  ['held off by another window', () => Response.json({ code: 'active_run' }, { status: 409 }), 1],
  ['that got no answer', () => { throw new TypeError('fetch failed'); }, 1]]) {
  test(`a start of its own ${kind} sends one request; a later release here asks ${again ? 'once more' : 'nothing'} (design review B3)`, async (t) => {
    const conversation = `c-b3-${kind.split(' ').at(-1)}`;
    const run = backend({ read: () => record(`${conversation}-a`, 'succeeded'), starts: [answer],
      streams: { '/a': () => succeeded(`${conversation}-a`), '/b': () => Response.json({ code: 'no_provider' }, { status: 400 }) } });
    t.mock.method(globalThis, 'fetch', run.fetch);
    await send(conversation, '/a', {}, 'A');
    await settle();
    assert.equal(run.calls.queueSends.length, 1); // no loop: a refused start of its own asks nothing more itself
    const reads = run.calls.reads;
    assert.equal((await send(conversation, '/b', {}, 'B').catch((error) => error)).code, 'no_provider'); // free here again
    await settle();
    assert.ok(run.calls.reads > reads); // that release asked
    assert.equal(run.calls.queueSends.length, 1 + again);
  });
}

test('a stream cut while its answer is written starts nothing while the record says running; then the backend refuses the start (the cut stream stopped it) and it waits with Send (cycle 5 C2)', async (t) => {
  let status = 'running';
  const run = backend({ read: () => record('r-c5c2', status), streams: { '/a': () => new Response(streamOf(
    'data: {"type":"run_started","run_id":"r-c5c2"}\n\n')), '/b': () => Response.json({ code: 'no_provider' }, { status: 400 }) },
  starts: [() => Response.json({ code: 'not_after_success' }, { status: 409 }), () => succeeded('r-c5c2-sent')] });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-c5c2', '/a', {}, 'A');
  await settle();
  assert.deepEqual(run.calls.queueSends, []); // its end read the turn running: the answer's commit was still written
  status = 'succeeded'; // committed: the backend marked the turn stopped as its stream closed first
  await send('c-c5c2', '/b', {}, 'B').catch(() => {}); // a later release here asks once, and is refused
  await settle();
  await send('c-c5c2', '/b', {}, 'B').catch(() => {}); // and then asks nothing more
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-c5c2/queue/q1/send', { after: 'r-c5c2' }]]);
  await viewHandlers('c-c5c2').sendQueued({ id: 'q1', text: 'Next' }); // Send starts it
  await settle();
  assert.deepEqual(run.calls.queueSends.at(-1), ['/api/conversations/c-c5c2/queue/q1/send', {}]);
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

// PR 52 cycle 4 (G8, design review B2): the start names only the turn it follows, whose route the backend reads from that
// turn's record (runs.inputs), so neither the picker's choice now nor a default changed since can reach it.
test('a waiting message is started naming only the turn it follows, never the picker\'s choice; after a turn sent elsewhere, nothing starts here (D15)', async (t) => {
  let latest = 'r-d15';
  const run = backend({ streams: { '/x': () => succeeded('r-d15') }, read: () => record(latest, 'succeeded') });
  t.mock.method(globalThis, 'fetch', run.fetch);
  await send('c-d15', '/x', { content: 'Q', model: 'm-a', provider: 'openrouter', effort: 'high', material_ids: ['p'] }, 'Q');
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-d15/queue/q1/send', { after: 'r-d15' }]]);
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

test('a $0 cap shows the bar, over it at any spend; a limit the file does not give shows none (G2; PR 52 cycle 4, G4)', () => {
  const zero = (settled) => ({ ...spend(1, 0), project: { limit_usd: 0, settled_usd: settled, open_usd: 0 } });
  const over = budgetBar(zero(1)); // $1 of the conversation's $10, and $1 settled against a $0 project budget
  assert.deepEqual([over.budget, over.settled, over.limit, over.meter], ['project', 1, 0, 100]);
  assert.deepEqual([budgetBar(zero(0)).budget, budgetBar(zero(0)).meter], ['project', 100]); // at it, nothing spent
  assert.equal(budgetBar({ ...spend(1, 0), project: { limit_usd: null, settled_usd: 5, open_usd: 0 } }), null);
});

// Harold, 2026-11-12 (variant 1N, which narrows reading 12 and replaces cycle 2's D22): a steer whose turn had ended waits
// queued, as the backend keeps it, and starts only as any waiting message does: after a turn that succeeded here (drain),
// which the backend refuses after a Stop from any window; after any other ending it waits with Send.
test('a steer whose turn had ended waits queued and starts only as any waiting message does (D22 under variant 1N)', () => {
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.ok(!/sent\.ended|sendQueued|idle\(|halt\(/.test(followUp), followUp);
  assert.match(followUp, /\n\s*if \(sent\.queued\) drain\(id\);/);
});

// PR 52's follow-up: E4's rule for the personal file too, whose default conversation budget the ask names instead.
test('a budget ask whose budget could not be read says so, never that the budget is $0 (E4), in both catalogs', () => {
  const ask = readFileSync(fileURLToPath(new URL('../src/components/Ask.jsx', import.meta.url)), 'utf8');
  assert.match(ask, /budget\?\.unread \? t\(budget\.budget === 'conversation' \? 'ask\.budget\.unreadConversation' : 'ask\.budget\.unread'\)/);
  for (const language of ['en', 'zh-CN']) {
    const catalog = JSON.parse(readFileSync(fileURLToPath(new URL(`../src/i18n/${language}.json`, import.meta.url)), 'utf8'));
    for (const key of ['ask.budget.unread', 'ask.budget.unreadConversation']) {
      assert.ok(catalog[key] && !/\{limit\}/.test(catalog[key]), `${language} ${key}`);
    }
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

// PR 52 cycle 4 (design review B2): a seeded random matrix. The view's handlers and live.js run against a backend that
// follows the backend's rules (one turn at a time; a start after a turn only while that turn is the latest, succeeded
// and not stopped; a steer for an ended turn queued and answered "ended"; Stop marks the run), where every response, the
// saved choice's read and every stream event arrive when the schedule releases them, in random order; Stop here and in
// another window, the researcher's messages, steers, Sends and the turns' endings come in between. Checked: no request
// leaves for an action the researcher took before a Stop here; nothing starts on its own after a Stop here until a turn
// the researcher started is admitted; nothing starts on its own but through the drain; at most one start a turn, the
// oldest waiting; and once all has arrived, a waiting message never waits behind a turn that succeeded here with no Stop.
// PR 52 cycle 5 (design review B3): waiting messages are also edited or removed (one being started then is refused
// not_queued) and the researcher's own sends are refused on their merits at random; the one start not owed after a turn
// is after three edits or removals in a row (a listed rule case).
function seeded(seed) {
  return () => { seed = (seed + 0x6d2b79f5) | 0; let x = Math.imul(seed ^ (seed >>> 15), 1 | seed); x ^= x + Math.imul(x ^ (x >>> 7), 61 | x); return ((x ^ (x >>> 14)) >>> 0) / 4294967296; };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function trial(seed, install) {
  const random = seeded(seed);
  const pick = (items) => items[Math.floor(random() * items.length)];
  const conversation = `c-matrix-${seed}`;
  const broken = [];
  const state = { turns: [], queued: [], stopped: new Set(), runs: 0, items: 0, halted: false, started: new Map(), pressed: new Map(),
    pending: [], streams: new Map(), answers: new Map(), starting: new Set() };
  const latest = () => state.turns.at(-1);
  const running = () => state.turns.find((turn) => turn.status === 'running');
  const quietly = (fn) => { try { fn(); } catch { /* the stream was closed already: its reader stopped */ } };
  const inOrder = (s, fn) => { s.queue.push(fn); state.pending.push(() => s.queue.shift()?.()); }; // one stream's events
  const later = (fn) => new Promise((resolve, reject) => state.pending.push(() => { try { resolve(fn()); } catch (error) { reject(error); } }));
  const end = (turn, status) => { // the run's terminal record, then its stream says so
    if (turn.status !== 'running') return;
    turn.status = status;
    const s = state.streams.get(turn.run_id);
    inOrder(s, () => { quietly(() => s.event(`{"type":"run_finished","status":"${status}"}`)); quietly(() => s.close()); });
  };
  const admit = (init, queued) => {
    if (running()) return Response.json({ code: 'active_run' }, { status: 409 });
    const turn = { run_id: `${conversation}-r${state.runs += 1}`, status: 'running' };
    if (queued) state.queued = state.queued.filter((item) => item !== queued);
    state.turns.push(turn);
    const s = Object.assign(controlledStream(), { queue: [] });
    state.streams.set(turn.run_id, s);
    init.signal?.addEventListener('abort', () => { end(turn, 'cancelled'); quietly(() => s.close()); }); // closed: stopped
    inOrder(s, () => quietly(() => s.event(`{"type":"run_started","run_id":"${turn.run_id}"}`)));
    return new Response(s.body);
  };
  const fetch = (url, init = {}) => {
    url = String(url);
    const body = init.body ? JSON.parse(init.body) : {};
    const sending = /message\/stream|\/continue|\/queue\/[^/]+\/send/.test(url);
    const version = url.includes('/queue/') ? state.queued.find((q) => q.id === url.split('/').at(-2))?.version : undefined;
    if (url.includes('/queue/')) state.starting.add(url.split('/').at(-2));
    if (sending && body.after) { // a start of its own, checked as it leaves
      if (state.halted) broken.push(`${url}: started on its own after a Stop here`);
    } else if (sending) {
      const key = url.includes('/queue/') ? url.split('/').at(-2) : body.content ?? 'continue';
      const presses = state.pressed.get(key) ?? []; // the Stop count at each press of it not yet sent
      const at = presses.indexOf(stopsAt(conversation));
      if (!presses.length) broken.push(`${url}: started with no action of the researcher's`);
      else if (at < 0) broken.push(`${url}: left after a Stop pressed since its action`);
      else presses.splice(at, 1);
    }
    return later(() => {
      if (init.signal?.aborted) throw abortError();
      if (url.endsWith('/api/settings')) return Response.json({ values: { ui: { follow_up: state.mode } } });
      if (url.includes('/cancel')) {
        const turn = state.turns.find((t) => url.includes(t.run_id));
        if (turn) { state.stopped.add(turn.run_id); end(turn, 'cancelled'); }
        return Response.json({ status: 'cancelled' });
      }
      if (url.endsWith('/steering')) {
        const turn = state.turns.find((t) => url.includes(t.run_id));
        if (turn?.status === 'running') return Response.json({ steering: { run_id: turn.run_id, seq: 1 } });
        const item = { id: `q${state.items += 1}`, text: body.content, version: 0 };
        state.queued.push(item);
        return Response.json({ queued: item, ended: true });
      }
      if (url.endsWith('/queue')) {
        const item = { id: `q${state.items += 1}`, text: body.content, version: 0 };
        state.queued.push(item);
        return Response.json(item, { status: 201 });
      }
      if (url.includes('/queue/')) {
        state.starting.delete(url.split('/').at(-2));
        const item = state.queued.find((q) => q.id === url.split('/').at(-2));
        const answered = (code) => { // the answers to the starts after a turn, in order
          if (body.after) state.answers.set(body.after, [...(state.answers.get(body.after) ?? []), code]);
          return Response.json({ code }, { status: code === 'refused' ? 400 : 409 });
        };
        if (body.after) {
          const before = latest();
          if (!before || before.run_id !== body.after || before.status !== 'succeeded' || state.stopped.has(body.after)) {
            return answered('not_after_success');
          }
        }
        if (!item || item.version !== version) return answered('not_queued'); // removed, or edited as it was started
        if (running()) return answered('active_run');
        if (!body.after && random() < 0.2) return answered('refused'); // the researcher's own Send, refused on its merits
        if (body.after) {
          state.started.set(body.after, (state.started.get(body.after) ?? 0) + 1);
          if (state.started.get(body.after) > 1) broken.push(`two starts after ${body.after}`);
          if (state.queued[0] !== item) broken.push(`${item.id} started before an older waiting message`);
        }
        const response = admit(init, item);
        if (response.ok && !body.after) state.halted = false; // a turn the researcher started (Send), admitted
        return response;
      }
      if (sending) {
        if (random() < 0.2) return Response.json({ code: 'refused' }, { status: 400 }); // refused on its merits
        const response = admit(init);
        if (response.ok) state.halted = false; // a turn the researcher started, admitted
        return response;
      }
      return Response.json({ turns: state.turns.map((t) => ({ ...t })), queued: state.queued.map((q) => ({ ...q })) });
    });
  };
  install(fetch);
  const press = (key) => state.pressed.set(key, [...(state.pressed.get(key) ?? []), stopsAt(conversation)]);
  let shownTurn = null;
  const unsubscribe = subscribe((id, turn) => { if (id === conversation) shownTurn = turn; });
  const view = () => {
    const live_ = shownTurn;
    const isRunning = Boolean(live_ && !live_.done) || Boolean(running());
    return viewHandlers(conversation, { live: live_, running: isRunning, shown: state.turns.map((t) => ({ ...t })),
      readChoice: () => (random() < 0.5 ? Promise.resolve() : later(() => {})) });
  };
  const changed = () => { // a waiting message to edit or remove: one being started, when there is one
    const starting = state.queued.filter((q) => state.starting.has(q.id));
    return starting.length ? pick(starting) : state.queued.length ? pick(state.queued) : null;
  };
  const actions = {
    message: () => { const text = `m${state.items += 1}`; press(text); view().submit(text); },
    send: () => { if (!state.queued.length) return; const item = pick(state.queued); press(item.id); view().sendQueued(item); },
    edit: () => { const item = changed(); if (item) { item.text += '*'; item.version += 1; } },
    remove: () => { const item = changed(); if (item) state.queued = state.queued.filter((q) => q !== item); },
    stop: () => { state.halted = true; stop(conversation, running()?.run_id).catch(() => {}); },
    stopElsewhere: () => { const turn = latest(); if (turn) { state.stopped.add(turn.run_id); end(turn, 'cancelled'); } },
    finish: () => { const turn = running(); if (turn) end(turn, pick(['succeeded', 'succeeded', 'failed'])); },
    mode: () => { state.mode = pick(['steer', 'queue']); },
    deliver: () => { if (state.pending.length) state.pending.splice(Math.floor(random() * state.pending.length), 1)[0](); },
  };
  state.mode = pick(['steer', 'queue']);
  const weights = ['message', 'message', 'send', 'stop', 'stopElsewhere', 'finish', 'finish', 'mode', 'edit', 'remove', 'deliver',
    'deliver', 'deliver', 'deliver', 'deliver', 'deliver', 'deliver'];
  for (let step = 0; step < 40; step += 1) {
    actions[pick(weights)]();
    await tick();
  }
  for (let rounds = 0; rounds < 200 && (state.pending.length || running()); rounds += 1) { // everything arrives
    if (state.pending.length) state.pending.shift()(); else end(running(), 'succeeded');
    await tick(); await tick();
  }
  for (let i = 0; i < 5; i += 1) await tick();
  const last = latest();
  const givenUp = (state.answers.get(last?.run_id) ?? []).slice(-3);
  if (!state.pending.length && !running() && last?.status === 'succeeded' && !state.halted && !state.stopped.has(last.run_id)
    && state.queued.length && !state.started.get(last.run_id) && !(givenUp.length === 3 && givenUp.every((c) => c === 'not_queued'))) {
    broken.push(`${state.queued.map((q) => q.id)} left waiting behind ${last.run_id}, which succeeded here`);
  }
  unsubscribe();
  return broken;
}

test('a seeded random matrix of sends, steers, Sends, edits, removals, refusals, Stops here and elsewhere and late deliveries keeps every rule (B2, B3)', async (t) => {
  let current = null;
  t.mock.method(globalThis, 'fetch', (...args) => current(...args));
  const found = [];
  // seeds 1 to 400, and 1059: cycle 5's C1 on 82e95ab (a start given up while a Send was pending, then that Send refused)
  for (const seed of [...Array.from({ length: 400 }, (_, i) => i + 1), 1059]) {
    found.push(...(await trial(seed, (fetch) => { current = fetch; })).map((problem) => `seed ${seed}: ${problem}`));
  }
  assert.deepEqual(found.slice(0, 10), []);
});

// PR 52's follow-up (cycle 5 A-F2): a share of a budget is settled / limit, which a limit too large to multiply cannot
// overflow: 1e307 of 1.7e308 is about 5.9%, never Infinity with a full bar.
test('the bar reads the share of a limit too large to multiply, never Infinity (A-F2)', () => {
  const huge = (settled) => ({ conversation: { limit_usd: 1.7e308, settled_usd: settled, open_usd: 0, own: true },
    project: { limit_usd: 1.7e308, settled_usd: settled, open_usd: 0 }, notice_percents: [50, 85, 90] });
  assert.equal(budgetBar(huge(1e307)), null); // about 5.9%: below the lowest notice percent
  const near = budgetBar(huge(1.6e308));
  assert.ok(Math.abs(near.percent - 94.1176) < 0.001 && near.meter === 94, JSON.stringify(near));
  assert.equal(budgetBar(huge(Number.MAX_VALUE)).meter, 100); // a total held at the most a float holds: over it
});

// PR 52's follow-up (cycle 5 C3; SPEC:415, F4): the view's conversation read, taken from its source and run with what it
// closes over stood in for. An older read answering after a queue edit's or removal's newer one was shown over it,
// putting the removed or old text back on screen; only the newest read asked is shown, as the asks' are.
test('an older conversation read answering late never restores a waiting message edited or removed since (C3)', async () => {
  const { newest, readGuard } = await import('../src/library.js');
  const source = VIEW();
  const start = source.indexOf('const load = useCallback(async () => {');
  let end = source.indexOf('{', start);
  for (let depth = 0; ; end += 1) {
    depth += source[end] === '{' ? 1 : source[end] === '}' ? -1 : 0;
    if (depth === 0) break;
  }
  const body = source.slice(source.indexOf('async () =>', start), end + 1);
  for (const [label, after] of [['removed', []], ['edited', [{ id: 'q1', text: 'Edited 中文' }]]]) {
    const reads = [];
    const shown = { queued: null };
    const env = { id: 'c-c3', guard: { current: readGuard() }, newestRead: newest(), markdownReady: async () => {},
      get: () => new Promise((resolve) => reads.push(resolve)), setTurns: () => {},
      setQueued: (queued) => { shown.queued = queued; }, setFailed: () => {}, setStale: () => {}, setReads: () => {} };
    const load = new Function(...Object.keys(env), `return (${body});`)(...Object.values(env));
    const older = load(); // asked before the edit or removal, held
    const newer = load(); // the change's own read
    await new Promise((resolve) => setImmediate(resolve));
    reads[1]({ turns: [], queued: after });
    await newer;
    reads[0]({ turns: [], queued: [{ id: 'q1', text: 'Original 中文' }] });
    await older;
    assert.deepEqual(shown.queued, after, label);
  }
});

// The internal review's F2: a needed limit too large to count in cents is said as it is, never as "null".
test('a limit too low for the step says the least it needs even past what cents can count (A-F2)', async () => {
  const { ApiError } = await import('../src/api.js');
  const { problemText } = await import('../src/action.js');
  const { makeT } = await import('../src/i18n/index.js');
  for (const needed of [1.7e307, Number.MAX_VALUE]) {
    const text = problemText(makeT('en'), new ApiError(400, 'limit_too_low', needed), 'en');
    assert.ok(!/null|Infinity|NaN/.test(text) && /\$\d{1,3}(,\d{3})+/.test(text), text);
  }
});
