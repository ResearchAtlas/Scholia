// S1-33 (F11): the budget bar's figures, a message sent while a turn runs (as the one setting says, never a choice in
// the message box), and which queued message a finished turn starts.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { budgetBar, follow, nextQueued } from '../src/followup.js';
import { drain, send, stop } from '../src/live.js';

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
