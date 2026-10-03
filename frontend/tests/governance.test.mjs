// What a project holds, changes that ask for confirmation, and listings per project
// (slice-1 spec F1, sections 5 and 6.4).
import test from 'node:test';
import assert from 'node:assert/strict';
import { HOLDS, SUGGESTED_HOLDS, holdsBody, holdsOf } from '../src/projects.js';
import { ApiError, confirmedChange, del } from '../src/api.js';
import { forgetModels, loadModels } from '../src/settings.js';

test('the three answers set the level, and the review answer the lock and its venue', () => {
  assert.deepEqual(HOLDS, ['own', 'private', 'review']);
  assert.equal(SUGGESTED_HOLDS, 'private'); // the social-science default suggestion
  assert.deepEqual(holdsBody('own'), { sensitivity: 'normal' });
  assert.deepEqual(holdsBody('private', 'ignored'), { sensitivity: 'private' });
  assert.deepEqual(holdsBody('review', '  Sage  '), { sensitivity: 'local_only', review_lock: true, review_venue: 'Sage' });
  assert.deepEqual(holdsBody('review', '  '), { sensitivity: 'local_only', review_lock: true, review_venue: null });
});

test('a project reads back as its answer, and Local only without the lock as none', () => {
  assert.equal(holdsOf({ sensitivity: 'normal', review_lock: false }), 'own');
  assert.equal(holdsOf({ sensitivity: 'private', review_lock: false }), 'private');
  assert.equal(holdsOf({ sensitivity: 'local_only', review_lock: true }), 'review');
  assert.equal(holdsOf({ sensitivity: 'local_only', review_lock: false }), null);
});

test('a change that needs confirmation is sent again with its token only when confirmed', async () => {
  const sent = [];
  const send = async (token) => {
    sent.push(token);
    if (!token) throw new ApiError(409, 'confirmation_required', { code: 'confirmation_required', token: 't1' });
    return { ok: true };
  };
  assert.equal(await confirmedChange(send, async () => false), null);
  assert.deepEqual(sent, [null]); // declined: sent once, never with the token
  assert.deepEqual(await confirmedChange(send, async () => true), { ok: true });
  assert.deepEqual(sent, [null, null, 't1']);
  await assert.rejects(confirmedChange(async () => { throw new ApiError(400, 'general_project'); }, async () => true),
    (error) => error.code === 'general_project');
  assert.deepEqual(await confirmedChange(async () => ({ applied: true }), async () => assert.fail('not asked')),
    { applied: true }); // a stricter change goes ahead without asking
});

test('a refusal keeps the whole answer, with its token', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ code: 'confirmation_required', token: 'abc' }, { status: 409 }));
  await assert.rejects(del('/api/audit'), (error) => error.code === 'confirmation_required' && error.data.token === 'abc');
});

test('listings are kept per project, each asking for that project\'s allowed models', async (t) => {
  const asked = [];
  t.mock.method(globalThis, 'fetch', async (path) => {
    asked.push(path);
    return Response.json({ models: [], status: {} });
  });
  forgetModels();
  await loadModels('openrouter');
  await loadModels('openrouter', { projectId: 'p1' });
  await loadModels('openrouter', { projectId: 'p1' });
  await loadModels('openrouter', { projectId: 'p2' });
  assert.deepEqual(asked, ['/api/providers/openrouter/models', '/api/providers/openrouter/models?project_id=p1',
    '/api/providers/openrouter/models?project_id=p2']);
  forgetModels();
});
