// Design review AC: a 2xx whose body cannot be read is an answer that was lost, never an empty success, for every
// caller of api() and confirmedChange() (api.js). Real Response objects whose body stream fails; the real memory.js and
// the real DeleteDialog on the stand-in. Every answer is given by the scenario; nothing sleeps.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ApiError, api, confirmedChange, del, get, patch, post, put } from '../src/api.js';
import { makeT } from '../src/i18n/index.js';
import { attempt, forgetRecord } from '../src/memory.js';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());
const t = makeT('en');

// headers arrive with this status, then the body's stream fails with `error`
const lost = (status = 200, error = new TypeError('network error')) => new Response(
  new ReadableStream({ start(controller) { controller.error(error); } }), { status, headers: { 'Content-Type': 'application/json' } });
const noAnswer = (error) => error instanceof ApiError && error.status === 0;
const answers = (t_, ...given) => { const sent = [];
  t_.mock.method(globalThis, 'fetch', async (path, init = {}) => { sent.push(`${init.method ?? 'GET'} ${path}`); return given.shift()(); });
  return sent; };

test('aTwoHundredWhoseBodyCannotBeReadIsNoAnswerForEveryVerb', async (t_) => {
  for (const call of [() => get('/api/x'), () => post('/api/x'), () => put('/api/x', {}), () => patch('/api/x', {}), () => del('/api/x'),
    () => api('DELETE', '/api/x')]) {
    answers(t_, () => lost(200));
    await assert.rejects(call(), noAnswer);
    t_.mock.restoreAll();
  }
  answers(t_, () => lost(201));
  await assert.rejects(post('/api/x'), noAnswer, 'any 2xx');
});

test('aChangeThatNeedsNoConfirmationWhoseAnswerWasLostIsNotTakenAsNotConfirmed', async (t_) => {
  answers(t_, () => lost(200));
  await assert.rejects(confirmedChange('POST', '/api/projects/p/sensitivity', { level: 'private' }, async () => assert.fail('not asked')), noAnswer);
});

test('aConfirmedChangeWhoseAnswerWasLostIsNotTakenAsNotConfirmed', async (t_) => {
  const sent = answers(t_, () => Response.json({ code: 'confirmation_required', token: 't1' }, { status: 409 }), () => lost(200));
  await assert.rejects(confirmedChange('DELETE', '/api/audit', undefined, async () => true), noAnswer);
  assert.deepEqual(sent, ['DELETE /api/audit', 'DELETE /api/audit?token=t1']);
});

test('aReadGivenUpWhileItsBodyCameIsGivenUpNotAnsweredEmpty', async (t_) => {
  answers(t_, () => lost(200, new DOMException('The operation was aborted.', 'AbortError')));
  await assert.rejects(get('/api/x', { signal: new AbortController().signal }), (error) => error.name === 'AbortError');
});

// Guards: green at the head.
test('whatAnAnswerSaysIsKept', async (t_) => {
  answers(t_, () => Response.json({ ok: true, purge_failed: true }), () => Response.json(null),
    () => lost(500), () => Response.json({ code: 'not_found' }, { status: 404 }), () => { throw new TypeError('Failed to fetch'); });
  assert.deepEqual(await del('/api/x'), { ok: true, purge_failed: true });
  assert.equal(await get('/api/x'), null, 'a body that says null is an answer');
  await assert.rejects(get('/api/x'), (error) => error.status === 500 && error.code === 'http_error', 'a failure keeps its status');
  await assert.rejects(get('/api/x'), (error) => error.status === 404 && error.code === 'not_found');
  await assert.rejects(get('/api/x'), noAnswer);
});

// r6-2's finding 1: memory's Delete everywhere.
const RECORD = { id: 'r1', version: 3 };
test('forgetEverywhereWhoseAnswerWasLostIsNotReportedAsDone', async (t_) => {
  const sent = answers(t_, () => lost(200));
  const failed = await attempt(t, () => forgetRecord(RECORD, true), false, 'r1');
  assert.deepEqual(sent, ['DELETE /api/memory/r1?version=3&purge_backups=true']);
  assert.notEqual(failed, null, 'not a success with nothing to say');
  assert.equal(failed.code, 'unreachable');
  assert.equal(failed.problem, "Scholia's engine is not responding. Close Scholia and open it again.");
});
test('forgetEverywhereThatCouldNotReplaceTheBackupsSaysSo (guard)', async (t_) => {
  answers(t_, () => Response.json({ ok: true, purge_failed: true }));
  const failed = await attempt(t, () => forgetRecord(RECORD, true), false, 'r1');
  assert.equal(failed.problem, 'It was deleted, but the older backups could not be replaced. They keep it until they are replaced in turn.');
});
test('forgetEverywhereThatWasDoneIsDone (guard)', async (t_) => {
  answers(t_, () => Response.json({ ok: true, purged: 2 }));
  assert.equal(await attempt(t, () => forgetRecord(RECORD, true), false, 'r1'), null);
});

// This review: the delete dialog (a project, a conversation, a paper). At the head it says nothing at all.
async function dialog(t_, strict, given) {
  const { mount, jsx } = await load.fresh();
  const { DeleteDialog } = await load.component('DeleteDialog');
  const seen = { settled: [], done: [], closed: 0 };
  const sent = answers(t_, given);
  const ui = await mount([jsx(DeleteDialog, { target: { kind: 'project', id: 'p1', title: 'Delete it?', body: '' },
    onClose: () => { seen.closed += 1; }, onDone: (id) => seen.done.push(id), onSettled: (id, ok) => seen.settled.push([id, ok]) })], { strict }).settle();
  const press = async (label) => { const found = ui.find((props, type) => type === 'ui:Button').filter((h) => {
    const i = ui.hosts().indexOf(h); return ui.hosts()[i + 1]?.text === label; });
    assert.equal(found.length, 1, label); found[0].props.onClick(); await ui.settle(); };
  return { seen, sent, press, alerts: () => ui.said((props) => props.role === 'alert') };
}
for (const strict of [false, true]) {
  const mode = strict ? ' (StrictMode)' : '';
  test(`deleteEverywhereWhoseAnswerWasLostSaysSo${mode}`, async (t_) => {
    const d = await dialog(t_, strict, () => lost(200));
    await d.press('Delete everywhere including backups');
    assert.deepEqual(d.sent, ['DELETE /api/projects/p1?purge_backups=true']);
    assert.deepEqual(d.alerts(), ["Scholia's engine is not responding. Close Scholia and open it again."]);
    assert.deepEqual(d.seen.settled, [['p1', false]], 'the window reads again and leaves it on its 404 (rider N)');
    assert.deepEqual(d.seen.done, []);
  });
  test(`deleteEverywhereThatCouldNotReplaceTheBackupsSaysSo (guard)${mode}`, async (t_) => {
    const d = await dialog(t_, strict, () => Response.json({ ok: true, purge_failed: true }));
    await d.press('Delete everywhere including backups');
    assert.deepEqual(d.alerts(), ['It was deleted, but the older backups could not be replaced. They keep it until they are replaced in turn.']);
    assert.deepEqual(d.seen.settled, [['p1', true]]);
  });
}

// The census: every request is made in api.js, and an answer's JSON is parsed in one place there (answer), so no caller
// can take a lost body as an empty success. A blob's and a stream's readers already fail when their body does.
test('everyAnswerIsParsedInOnePlace', () => {
  const SRC = fileURLToPath(new URL('../src/', import.meta.url));
  const found = [];
  for (const file of readdirSync(SRC, { recursive: true })) {
    if (!/\.jsx?$/.test(file)) continue;
    const code = readFileSync(join(SRC, file), 'utf8').replace(/^\s*\/\/.*$/gm, '');
    for (const match of code.matchAll(/\.json\(\)|\bfetch\(/g)) found.push(`${file}: ${match[0]}`);
  }
  assert.deepEqual(found.sort(), ['api.js: .json()', 'api.js: fetch(', 'api.js: fetch(', 'api.js: fetch(', 'api.js: fetch(']);
});
