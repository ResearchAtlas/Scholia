import test from 'node:test';
import assert from 'node:assert/strict';
import { groupOf, loadModels, forgetModels, messageRoute, settingKey, utf8Bytes, valueAt } from '../src/settings.js';

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

test('a message is sent with the chosen model and its effort, or Auto', () => {
  assert.deepEqual(messageRoute(null), { model: 'auto' });
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
