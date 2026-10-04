// What a project holds, changes that ask for confirmation, and listings per project
// (slice-1 spec F1, sections 5 and 6.4).
import test from 'node:test';
import assert from 'node:assert/strict';
import { HOLDS, SUGGESTED_HOLDS, holdsBody, holdsOf, moveTargets, tightens } from '../src/projects.js';
import { ApiError, confirmedChange } from '../src/api.js';
import { forgetModels, keptModels, loadModels } from '../src/settings.js';
import { auditDetail, auditEvent } from '../src/audit.js';
import { makeT } from '../src/i18n/index.js';

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

test('only a stricter answer waits to apply, with its pending state shown', () => {
  const normal = { sensitivity: 'normal', review_lock: false };
  const privateProject = { sensitivity: 'private', review_lock: false };
  const localOnly = { sensitivity: 'local_only', review_lock: false };
  assert.equal(tightens(normal, 'private'), true);
  assert.equal(tightens(normal, 'review'), true);
  assert.equal(tightens(privateProject, 'review'), true);
  assert.equal(tightens(localOnly, 'review'), true); // the lock on top of Local only
  assert.equal(tightens(privateProject, 'own'), false);
  assert.equal(tightens(localOnly, 'private'), false);
  assert.equal(tightens({ sensitivity: 'local_only', review_lock: true }, 'review'), false);
});

test('a review-locked project\'s conversations move only to another locked project', () => {
  const projects = [{ id: 'n', sensitivity: 'normal' }, { id: 'l', sensitivity: 'local_only', review_lock: false },
    { id: 'r', sensitivity: 'local_only', review_lock: true }, { id: 's', sensitivity: 'local_only', review_lock: true }];
  assert.deepEqual(moveTargets(projects, projects[2]).map((p) => p.id), ['s']);
  assert.deepEqual(moveTargets(projects, projects[1]).map((p) => p.id), ['r', 's']);
});

test('a change that needs confirmation is sent again with its token only when confirmed', async (t) => {
  const sent = [];
  t.mock.method(globalThis, 'fetch', async (path, { method, body }) => {
    const parsed = body && JSON.parse(body);
    sent.push([method, path, parsed]);
    if (parsed?.token === 't1' || path.endsWith('?token=t1')) return Response.json({ ok: true });
    return Response.json({ code: 'confirmation_required', token: 't1' }, { status: 409 });
  });
  assert.equal(await confirmedChange('POST', '/api/projects/p/sensitivity', { level: 'normal' }, async () => false), null);
  assert.deepEqual(sent, [['POST', '/api/projects/p/sensitivity', { level: 'normal' }]]); // declined: sent once
  assert.deepEqual(await confirmedChange('POST', '/api/projects/p/sensitivity', { level: 'normal' }, async () => true),
    { ok: true });
  assert.deepEqual(sent.at(-1), ['POST', '/api/projects/p/sensitivity', { level: 'normal', token: 't1' }]);
  assert.deepEqual(await confirmedChange('DELETE', '/api/audit', undefined, async () => true), { ok: true });
  assert.deepEqual(sent.at(-1), ['DELETE', '/api/audit?token=t1', undefined]); // a DELETE's token goes in its query
});

test('a change that needs no confirmation goes ahead, and other refusals are thrown', async (t) => {
  t.mock.method(globalThis, 'fetch', async (path) => (path.includes('general')
    ? Response.json({ code: 'general_project' }, { status: 400 }) : Response.json({ applied: true })));
  assert.deepEqual(await confirmedChange('POST', '/api/projects/p/sensitivity', { level: 'private' },
    async () => assert.fail('not asked')), { applied: true }); // a stricter change goes ahead without asking
  await assert.rejects(confirmedChange('POST', '/api/projects/general/sensitivity', { level: 'private' }, async () => true),
    (error) => error instanceof ApiError && error.code === 'general_project');
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

test('a provider\'s earlier rows are kept when only its listing failed, never across a change of protection', () => {
  const a = { provider: 'openrouter', id: 'a' };
  const b = { provider: 'local', id: 'b' };
  const before = { protection: 'normal', models: [a, b] };
  assert.deepEqual(keptModels(before, { protection: 'normal', models: [b] }, new Set(['openrouter'])), [b, a]);
  assert.deepEqual(keptModels(before, { protection: 'private', models: [b] }, new Set(['openrouter'])), [b]); // tightened
  assert.deepEqual(keptModels(before, null), []); // nothing could be read: nothing is offered
  assert.deepEqual(keptModels(null, { protection: 'private', models: [b] }, new Set(['openrouter'])), [b]);
});

test('an audit entry\'s heading says no more than its record knows, in each interface language', () => {
  const dates = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' });
  const zh = makeT('zh-CN');
  const en = makeT('en');
  const undone = { event: 'restore', data: { id: 'r1', interrupted: true, finished: 'back' } };
  const finished = { event: 'restore', data: { id: 'r1', interrupted: true, finished: 'forward' } };
  assert.equal(auditEvent(en, undone), 'Restore undone at launch');
  assert.equal(auditEvent(zh, undone), '启动时撤销了恢复');
  assert.equal(auditEvent(en, finished), 'Backup restored');
  assert.equal(auditDetail(en, undone, dates), 'id: r1 · interrupted by a crash: yes · at the next launch: undone');
  assert.equal(auditDetail(zh, finished, dates), 'ID：r1 · 因崩溃中断：是 · 下次启动时：已完成');
  assert.equal(auditEvent(en, { event: 'key_changed', data: { provider: 'openrouter', stored_in: 'uncertain' } }),
    'Key may have changed');
  assert.equal(auditEvent(en, { event: 'key_changed', data: { provider: 'openrouter', stored_in: 'file' } }), 'Key changed');
  // Recorded before anything is written, and kept when the writing fails: an attempt, not a result.
  for (const event of ['full_backup', 'project_export', 'audit_exported']) {
    assert.match(auditEvent(en, { event }), /started$/);
    assert.match(auditEvent(zh, { event }), /^开始/);
  }
  assert.equal(auditEvent(en, { event: 'later_event' }), 'later_event');
});

test('audit details are shown in the interface language, codes this version does not name as they are', () => {
  const dates = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' });
  const zh = makeT('zh-CN');
  const en = makeT('en');
  const change = { event: 'sensitivity_changed', data: { from: 'normal', to: 'private', revoked_runs: 0 } };
  assert.equal(auditDetail(zh, change, dates), '从：普通 · 到：私密 · 停止的运行：0');
  assert.equal(auditDetail(en, change, dates), 'from: Normal · to: Private · runs stopped: 0');
  assert.equal(auditDetail(zh, { event: 'outbound', data: { decision: 'deny', reason: 'key_not_confirmed',
    kind: 'model_provider', destination: 'https://openrouter.ai:443' } }, dates),
  '已拒绝：密钥设置未确认 · 模型服务商 · https://openrouter.ai:443');
  assert.equal(auditDetail(en, { event: 'review_lock_changed', data: { locked: true, venue_set: false } }, dates),
    'locked: yes · venue given: no');
  assert.equal(auditDetail(en, { event: 'deletion', data: { kind: 'conversation', deleted: { runs: 2, turns: 2 } } }, dates),
    'what: conversation · records removed: 4');
  assert.equal(auditDetail(en, { event: 'private_route_changed', data: { route: 'openrouter:x/y', enabled: null } }, dates),
    'route: openrouter:x/y');
  assert.equal(auditDetail(zh, { event: 'project_export', data: { file: '/Users/me/out/p.zip', encrypted: false,
    conversations: 1, turns: 1, artifacts: 0, materials: 0 } }, dates),
  '文件：/Users/me/out/p.zip · 已加密：否 · 对话：1 · 对话轮次：1 · 文稿：0 · 资料：0');
  assert.equal(auditDetail(en, { event: 'restore', data: { source: 'automatic', backup: 'daily/x', safety_copy: 'daily/y',
    damaged_copy: null, missing_files: 0 } }, dates),
  'restored from: an automatic backup · backup: daily/x · copy of the state before: daily/y · files missing: 0');
  assert.equal(auditDetail(en, { event: 'full_backup', data: { settings_left_out: ['config.toml'], projects: 2 } }, dates),
    'settings files left out: 1 · projects: 2');
  assert.equal(auditDetail(en, { event: 'later_event', data: { newer_field: 'code' } }, dates), 'newer_field: code');
  assert.equal(auditDetail(en, { event: 'outbound', data: { decision: 'deny', reason: 'a_newer_reason' } }, dates),
    'Refused: a_newer_reason');
});
