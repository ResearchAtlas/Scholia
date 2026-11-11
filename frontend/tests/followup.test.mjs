// S1-33 (F11): the budget bar's figures, a message sent while a turn runs (as the one setting says, never a choice in
// the message box), and which queued message a finished turn starts.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { budgetBar, follow, nextQueued } from '../src/followup.js';

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
