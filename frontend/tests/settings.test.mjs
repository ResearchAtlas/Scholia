import test from 'node:test';
import assert from 'node:assert/strict';
import { choiceUpdates, commitDecision, decodeChoice, onCatalogChange, groupOf, loadModels, forgetModels, messageRoute, saveAgainst, settingKey, settingsSaver, utf8Bytes, valueAt } from '../src/settings.js';
import { ApiError } from '../src/api.js';

test('settings keys quote the parts that are not bare TOML keys', () => {
  assert.equal(settingKey('providers', 'openrouter', 'models'), 'providers.openrouter.models');
  assert.equal(settingKey('providers', 'openrouter', 'windows', 'google/gemini-2.5-flash'),
    'providers.openrouter.windows."google/gemini-2.5-flash"');
  assert.equal(settingKey('models', 'efforts', 'a "quoted" id'), 'models.efforts."a \\"quoted\\" id"');
});

test('values are read at dotted keys', () => {
  assert.equal(valueAt({ subagents: { at_once: 3 } }, 'subagents.at_once'), 3);
  assert.equal(valueAt({ subagents: null }, 'subagents.at_once'), undefined);
});

test('providers are Ready, Needs setup or Off', () => {
  assert.equal(groupOf({ enabled: true, has_key: true }), 'ready');
  assert.equal(groupOf({ enabled: true, has_key: false }), 'setup');
  assert.equal(groupOf({ enabled: false, has_key: true }), 'off');
});

test('a message is sent with the chosen model and its effort, Auto, or the settings\' default', () => {
  assert.deepEqual(messageRoute(null), {}); // the project's or personal [models] default applies
  assert.deepEqual(messageRoute({ auto: true }), { model: 'auto' });
  assert.deepEqual(messageRoute({ provider: 'openrouter', model: 'a/b', effort: null }), { model: 'a/b', provider: 'openrouter' });
  assert.deepEqual(messageRoute({ provider: 'local', model: 'llama', effort: 'high' }),
    { model: 'llama', provider: 'local', effort: 'high' });
});

test('instructions are measured in UTF-8 bytes, as the 32 KiB cap counts them', () => {
  assert.equal(utf8Bytes('abc'), 3);
  assert.equal(utf8Bytes('队列'), 6);
  assert.equal(utf8Bytes(null), 0);
});

test('a provider\'s models are read once per window, again after a change, and a failure is not kept', async (t) => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => (++calls === 2
    ? Response.json({ code: 'network' }, { status: 502 }) : Response.json({ models: [{ id: 'a' }], status: {} })));
  forgetModels();
  await loadModels('openrouter');
  await loadModels('openrouter');
  assert.equal(calls, 1);
  forgetModels();
  await assert.rejects(loadModels('openrouter'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual((await loadModels('openrouter')).models, [{ id: 'a' }]); // the failure was dropped
  assert.equal(calls, 3);
});

test('a settings page saves against the file as it read it, never a fresher read', async (t) => {
  const sent = [];
  t.mock.method(globalThis, 'fetch', async (path, { method, body }) => {
    sent.push([method, path, body && JSON.parse(body)]);
    return Response.json({ code: 'settings_changed' }, { status: 409 });
  });
  await assert.rejects(saveAgainst({ hash: 'h-read' }, { 'limits.agent_steps': 20 }, 'p1'),
    (error) => error.code === 'settings_changed');
  assert.deepEqual(sent, [['PUT', '/api/settings', { hash: 'h-read', updates: { 'limits.agent_steps': 20 }, project_id: 'p1' }]]);
});

test('saves run in order against the latest file, and a conflict drops the saves made before it', async () => {
  let disk = { hash: 'h0', values: {} };
  const written = [];
  const saver = settingsSaver({
    read: async () => disk,
    write: async (hash, updates) => {
      if (hash !== disk.hash) throw new ApiError(409, 'settings_changed');
      written.push(updates);
      disk = { hash: `h${written.length}`, values: {} };
      return disk;
    },
    onFile: () => {},
    onProblem: () => {},
  });
  await saver.reload();
  assert.deepEqual(await Promise.all([saver.save({ a: 1 }), saver.save({ b: 2 })]), [true, true]); // each on the last
  disk = { hash: 'outside', values: {} }; // changed on disk
  const results = await Promise.all([saver.save({ c: 3 }), saver.save({ d: 4 })]);
  assert.deepEqual(results, [false, false]); // the conflict, and the save queued before it is known
  assert.deepEqual(written, [{ a: 1 }, { b: 2 }]);
  assert.equal(await saver.save({ e: 5 }), true); // a save made after the file was read again
  assert.deepEqual(written.at(-1), { e: 5 });
});

test('the picker\'s choice is kept as settings and read back, colons and all', () => {
  const asSettings = (updates) => ({ id: updates['ui.model.id'] ?? undefined, provider: updates['ui.model.provider'] ?? undefined });
  for (const choice of [{ auto: true }, { provider: 'openrouter', model: 'google/gemini-2.5-flash' },
    { provider: 'lab:v2', model: 'llama3:8b' }]) {
    assert.deepEqual(decodeChoice(asSettings(choiceUpdates(choice))), choice);
  }
  assert.deepEqual(choiceUpdates(null), { 'ui.model.id': null, 'ui.model.provider': null });
  assert.equal(decodeChoice(undefined), null);
  assert.equal(decodeChoice({ id: 'm' }), null); // a model without its provider is no choice
});

test('a change to the providers is announced; a reader that clears the cache itself is not', () => {
  let heard = 0;
  const stop = onCatalogChange(() => { heard += 1; });
  forgetModels();
  forgetModels({ quiet: true });
  stop();
  forgetModels();
  assert.equal(heard, 1);
});

test('a listing that reports an error is read again the next time', async (t) => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => (++calls === 1
    ? Response.json({ models: [], status: { error: 'refresh_failed' } }) : Response.json({ models: [{ id: 'a' }], status: {} })));
  forgetModels({ quiet: true });
  assert.equal((await loadModels('local')).status.error, 'refresh_failed');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual((await loadModels('local')).models, [{ id: 'a' }]);
  assert.equal(calls, 2);
});

test('a committed field saves what is new since its last commit, as the saved value will read', () => {
  const budget = { type: 'number', above: 0, value: 10, sent: null };
  assert.deepEqual(commitDecision('20', budget), { out: 20, shown: '20' });
  assert.deepEqual(commitDecision('10', { ...budget, sent: '20' }), { out: 10, shown: '10' }); // back to 10 while 20 saves
  assert.deepEqual(commitDecision('20.0', { ...budget, sent: '20' }), { same: true });
  assert.deepEqual(commitDecision('10', budget), { same: true });
  assert.deepEqual(commitDecision('0', budget), { reject: true, invalid: true });
  assert.deepEqual(commitDecision('', budget), { reject: true });
  assert.deepEqual(commitDecision('', { value: 'x', allowEmpty: true, sent: null }), { out: null, shown: '' });
  assert.deepEqual(commitDecision('2.5', { type: 'number', min: 1, step: 1, value: 3, sent: null }), { reject: true, invalid: true });
});
