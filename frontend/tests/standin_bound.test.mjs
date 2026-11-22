// The stand-in's request bound (recheck r8, finding 3; the batch I review): a loop of answers that are already there
// starves the event loop, so only a count can end it. Its settle then fails, saying so, instead of never returning.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mount, jsx } from './harness/standin.mjs';

test('a loop of answers that do not end fails the next settle', async () => {
  const kept = Object.getOwnPropertyDescriptor(globalThis, 'fetch');
  let asked = 0;
  globalThis.fetch = () => { asked += 1; return Promise.resolve({ ok: true, status: 200, json: async () => ({}) }); };
  try {
    const ui = mount(jsx(() => null, {}), { requests: 50 });
    const go = () => fetch('/api/x').then(go); // each answer asks again at once
    go();
    await assert.rejects(ui.settle(), /more than 50 requests \(the last: GET \/api\/x\): answers that do not end/);
    assert.equal(asked, 50); // past the bound none was asked of the backend
  } finally {
    if (kept) Object.defineProperty(globalThis, 'fetch', kept); else delete globalThis.fetch;
  }
});
