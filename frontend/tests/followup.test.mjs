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

// PR 52 review 1 (finding 4): a drain whose read was asked before a manual send started never starts its message, and a
// stream refused never clears or orphans a newer stream's state, so Stop still cancels the newer turn.
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
    if (url === '/manual') return new Response(manual.body);
    return Response.json({ run_id: 'r-manual', status: 'cancelled' });
  });
  const draining = drain('c-race', 'r1', {});
  await new Promise((resolve) => setTimeout(resolve, 5));
  await send('c-race', '/manual', {}, 'Typed now'); // admitted: its run started
  answerRead(Response.json({ turns: [{ run_id: 'r1', status: 'succeeded' }], queued: [{ id: 'q1', text: 'Next' }] }));
  await draining;
  assert.deepEqual(posts.filter((url) => url.includes('/queue/')), []);
  await stop('c-race');
  assert.ok(posts.includes('/api/runs/r-manual/cancel'));
  manual.close();
});

test('a stream refused after a newer one started leaves the newer one\'s state and Stop alone', async (t) => {
  const posts = [];
  let refuse;
  const newer = openStream('r-new');
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    if (init?.method === 'POST' && url.includes('/cancel')) posts.push(url);
    if (url === '/older') return new Promise((resolve) => { refuse = resolve; });
    if (url === '/newer') return new Response(newer.body);
    return Response.json({});
  });
  const older = send('c-two', '/older', {}, 'Older').catch((error) => error);
  await new Promise((resolve) => setTimeout(resolve, 5));
  await send('c-two', '/newer', {}, 'Newer');
  refuse(Response.json({ code: 'active_run' }, { status: 409 }));
  assert.equal((await older).code, 'active_run');
  await stop('c-two');
  assert.deepEqual(posts, ['/api/runs/r-new/cancel']);
  newer.close();
});

test('an older turn that finishes while a newer send is pending is shown finished when that send is refused', async (t) => {
  let refuse;
  const older = openStream('r-old');
  t.mock.method(globalThis, 'fetch', async (url) => {
    url = String(url);
    if (url === '/old') return new Response(older.body);
    if (url === '/pending') return new Promise((resolve) => { refuse = resolve; });
    return Response.json({ turns: [], queued: [] });
  });
  const seen = [];
  const unsubscribe = subscribe((id, turn) => { if (id === 'c-three') seen.push(turn); });
  await send('c-three', '/old', {}, 'Older');
  const pending = send('c-three', '/pending', {}, 'Newer').catch((error) => error);
  await new Promise((resolve) => setTimeout(resolve, 5));
  older.close(); // the older stream ends (its run finished) while the newer one is pending
  await new Promise((resolve) => setTimeout(resolve, 5));
  refuse(Response.json({ code: 'active_run' }, { status: 409 }));
  await pending;
  unsubscribe();
  const last = seen.at(-1);
  assert.equal(last?.runId, 'r-old');
  assert.equal(last?.done, true); // never left showing a turn that runs
});

test('three overlapping sends: when the later two are refused, the running turn keeps its state and Stop', async (t) => {
  const posts = [];
  const refusals = {};
  const running = openStream('r-run');
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    if (init?.method === 'POST' && url.includes('/cancel')) posts.push(url);
    if (url === '/run') return new Response(running.body);
    if (url === '/two' || url === '/three') return new Promise((resolve) => { refusals[url] = resolve; });
    return Response.json({});
  });
  await send('c-four', '/run', {}, 'Running');
  const two = send('c-four', '/two', {}, 'Two').catch((error) => error);
  const three = send('c-four', '/three', {}, 'Three').catch((error) => error);
  await new Promise((resolve) => setTimeout(resolve, 5));
  refusals['/two'](Response.json({ code: 'active_run' }, { status: 409 }));
  await two;
  refusals['/three'](Response.json({ code: 'active_run' }, { status: 409 }));
  await three;
  await stop('c-four');
  assert.deepEqual(posts, ['/api/runs/r-run/cancel']);
  running.close();
});

// Fix batch 1 (F1): an older stream whose first event has not been read yet may be an admitted turn: a refused newer
// stream gives the conversation back to it, so its events show and Stop reaches it.
function lateStream() { // an admitted turn whose events have not arrived
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  const encoder = new TextEncoder();
  return { body, event: (text) => controller.enqueue(encoder.encode(`data: ${text}\n\n`)), close: () => controller.close() };
}

test('a refused newer stream leaves an admitted turn whose run_started is unread showing, and Stop reaches it', async (t) => {
  const posts = [];
  const admitted = lateStream();
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    url = String(url);
    if (init?.method === 'POST' && url.includes('/cancel')) posts.push(url);
    if (url === '/admitted') return new Response(admitted.body);
    if (url === '/refused') return Response.json({ code: 'active_run' }, { status: 409 });
    return Response.json({});
  });
  const seen = [];
  const unsubscribe = subscribe((id, turn, kind) => { if (id === 'c-five') seen.push([kind, turn?.runId ?? null]); });
  send('c-five', '/admitted', {}, 'Admitted');
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal((await send('c-five', '/refused', {}, 'Refused').catch((error) => error)).code, 'active_run');
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

// Fix batch 2 (PR 52 cycle 2, D10 and D14): a send pending over a turn under way. A refused one gives the conversation
// back and starts the drain a turn that succeeded meanwhile left to it, once; a stopped one starts nothing; and Stop,
// pressed while it is pending, reaches every turn under way there, not only the latest stream.
function controlledStream() { // a stream whose events and end the test sends, each when it chooses
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  const encoder = new TextEncoder();
  return { body, event: (text) => controller.enqueue(encoder.encode(`data: ${text}\n\n`)), close: () => controller.close() };
}

function overlapping(conversation, turnStatus = 'succeeded') { // fetch for a turn /a and a send /b pending over it
  const calls = { queueSends: [], cancels: [], signals: {} };
  const a = controlledStream();
  let answerB;
  const fetch = async (url, init) => {
    url = String(url);
    if (url === '/a') { calls.signals.a = init.signal; return new Response(a.body); }
    if (url === '/b') {
      calls.signals.b = init.signal;
      return new Promise((resolve, reject) => {
        answerB = resolve;
        init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      });
    }
    if (url.includes('/cancel')) { calls.cancels.push(url); return Response.json({ status: 'cancelled' }); }
    if (url.includes('/queue/')) {
      calls.queueSends.push([url, JSON.parse(init.body)]);
      return new Response(streamOf('data: {"type":"run_started","run_id":"r-next"}\n\n', 'data: {"type":"run_finished","status":"succeeded"}\n\n'));
    }
    return Response.json({ turns: [{ run_id: 'r-a', status: turnStatus }], queued: [{ id: 'q1', text: 'Next' }] });
  };
  return { calls, a, fetch, refuseB: () => answerB(Response.json({ code: 'active_run' }, { status: 409 })) };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

test('a turn that succeeds while a newer send is pending starts its waiting message once that send is refused', async (t) => {
  const run = overlapping('c-d10a');
  t.mock.method(globalThis, 'fetch', run.fetch);
  const a = send('c-d10a', '/a', { model: 'm1', provider: 'openrouter' }, 'A');
  run.a.event('{"type":"run_started","run_id":"r-a"}');
  await a;
  const b = send('c-d10a', '/b', { model: 'm2' }, 'B').catch((error) => error);
  await settle();
  run.a.event('{"type":"run_finished","status":"succeeded"}');
  run.a.close(); // its own end runs while B is the conversation's
  await settle();
  run.refuseB();
  assert.equal((await b).code, 'active_run');
  await settle();
  assert.deepEqual(run.calls.queueSends, [['/api/conversations/c-d10a/queue/q1/send',
    { model: 'm1', provider: 'openrouter', after: 'r-a' }]]); // once, on the route the turn was sent on
});

test('a newer send refused after the turn\'s last event but before its own end leaves that end to drain, once', async (t) => {
  const run = overlapping('c-d10b');
  t.mock.method(globalThis, 'fetch', run.fetch);
  const a = send('c-d10b', '/a', { model: 'm1' }, 'A');
  run.a.event('{"type":"run_started","run_id":"r-a"}');
  await a;
  const b = send('c-d10b', '/b', {}, 'B').catch((error) => error);
  await settle();
  run.a.event('{"type":"run_finished","status":"succeeded"}'); // read, its stream not yet closed
  await settle();
  run.refuseB();
  await b;
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
  run.a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends.map(([url]) => url), ['/api/conversations/c-d10b/queue/q1/send']);
});

for (const status of ['failed', 'cancelled']) {
  test(`a turn that ends ${status} while a newer send is pending starts nothing when that send is refused`, async (t) => {
    const run = overlapping(`c-d10-${status}`, status);
    t.mock.method(globalThis, 'fetch', run.fetch);
    const a = send(`c-d10-${status}`, '/a', {}, 'A');
    run.a.event('{"type":"run_started","run_id":"r-a"}');
    await a;
    const b = send(`c-d10-${status}`, '/b', {}, 'B').catch((error) => error);
    await settle();
    run.a.event(`{"type":"run_finished","status":"${status}"}`);
    run.a.close();
    await settle();
    run.refuseB();
    await b;
    await settle();
    assert.deepEqual(run.calls.queueSends, []);
  });
}

test('Stop pressed while a newer send is pending stops the turn under way, and nothing waiting starts', async (t) => {
  const run = overlapping('c-d14a');
  t.mock.method(globalThis, 'fetch', run.fetch);
  const a = send('c-d14a', '/a', {}, 'A');
  run.a.event('{"type":"run_started","run_id":"r-a"}');
  await a;
  const b = send('c-d14a', '/b', {}, 'B').catch((error) => error);
  await settle();
  await stop('c-d14a');
  assert.deepEqual(run.calls.cancels, ['/api/runs/r-a/cancel']); // the turn that runs, not only the pending send
  assert.equal(run.calls.signals.b.aborted, true);
  await b;
  run.a.event('{"type":"run_finished","status":"cancelled"}');
  run.a.close();
  await settle();
  assert.deepEqual(run.calls.queueSends, []);
});

test('Stop pressed while a newer send is pending, after the turn under it succeeded, starts nothing', async (t) => {
  const run = overlapping('c-d14b');
  t.mock.method(globalThis, 'fetch', run.fetch);
  const a = send('c-d14b', '/a', {}, 'A');
  run.a.event('{"type":"run_started","run_id":"r-a"}');
  await a;
  const b = send('c-d14b', '/b', {}, 'B').catch((error) => error);
  await settle();
  run.a.event('{"type":"run_finished","status":"succeeded"}');
  run.a.close();
  await settle();
  await stop('c-d14b');
  assert.deepEqual(run.calls.cancels, []); // it has ended
  assert.equal(run.calls.signals.b.aborted, true);
  await b;
  await settle();
  assert.deepEqual(run.calls.queueSends, []); // the researcher stopped: the message waits with Send
});

test('Stop pressed while a newer send is pending closes a turn whose first event is unread, and a send still pending', async (t) => {
  for (const older of ['unread', 'pending']) {
    const signals = {};
    const unread = controlledStream();
    t.mock.method(globalThis, 'fetch', async (url, init) => {
      url = String(url);
      signals[url] = init.signal;
      if (url === '/older' && older === 'unread') return new Response(unread.body);
      return new Promise((resolve, reject) => init.signal.addEventListener('abort',
        () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))));
    });
    const conversation = `c-d14-${older}`;
    send(conversation, '/older', {}, 'Older').catch(() => {});
    await settle();
    const b = send(conversation, '/newer', {}, 'Newer').catch((error) => error);
    await settle();
    await stop(conversation);
    assert.equal(signals['/older'].aborted, true, older); // closing its stream stops a turn admitted or about to be
    assert.equal(signals['/newer'].aborted, true, older);
    await b;
    t.mock.restoreAll();
  }
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

test('an older send still pending is given the conversation back when a newer one is refused, and Stop reaches it', async (t) => {
  const cancels = [];
  const older = controlledStream();
  let admit;
  t.mock.method(globalThis, 'fetch', async (url) => {
    url = String(url);
    if (url.includes('/cancel')) { cancels.push(url); return Response.json({}); }
    if (url === '/older') return new Promise((resolve) => { admit = () => resolve(new Response(older.body)); });
    return Response.json({ code: 'active_run' }, { status: 409 });
  });
  const seen = [];
  const unsubscribe = subscribe((id, turn) => { if (id === 'c-pend') seen.push(turn?.runId ?? null); });
  send('c-pend', '/older', {}, 'Older').catch(() => {});
  await settle();
  assert.equal((await send('c-pend', '/newer', {}, 'Newer').catch((error) => error)).code, 'active_run');
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

test('a follow-up queued behind a turn drains on the route that turn was sent on, never the picker\'s choice now (D15)', async (t) => {
  const { sentRoute } = await import('../src/live.js');
  const stream = controlledStream();
  t.mock.method(globalThis, 'fetch', async () => new Response(stream.body));
  const sending = send('c-d15', '/x', { content: 'Q', model: 'm-a', provider: 'openrouter', effort: 'high', material_ids: ['p'] }, 'Q');
  stream.event('{"type":"run_started","run_id":"r-d15"}');
  await sending;
  assert.deepEqual(sentRoute('r-d15'), { model: 'm-a', provider: 'openrouter', effort: 'high' });
  assert.equal(sentRoute('r-unknown'), null); // a turn sent elsewhere: its own window drains it, or it waits with Send
  stream.close();
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.ok(!followUp.includes('currentChoice'), followUp);
  assert.match(followUp, /sentRoute\(after\)/);
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

test('a steer whose turn had ended starts as a new turn, as Send does, if the conversation is free (D22, reading 12)', () => {
  const followUp = VIEW().slice(VIEW().indexOf('async function followUp('), VIEW().indexOf('// Attached files'));
  assert.match(followUp, /if \(sent\.ended\) await sendQueued\(sent\.queued\)/);
});

test('a budget ask whose project budget could not be read says so, never that the budget is $0 (E4), in both catalogs', () => {
  const ask = readFileSync(fileURLToPath(new URL('../src/components/Ask.jsx', import.meta.url)), 'utf8');
  assert.match(ask, /budget\?\.unread \? t\('ask\.budget\.unread'\)/);
  for (const language of ['en', 'zh-CN']) {
    const catalog = JSON.parse(readFileSync(fileURLToPath(new URL(`../src/i18n/${language}.json`, import.meta.url)), 'utf8'));
    assert.ok(catalog['ask.budget.unread'] && !/\{limit\}/.test(catalog['ask.budget.unread']), language);
  }
});
