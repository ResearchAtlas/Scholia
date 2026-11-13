// The Library's and the message box's hooks run as written, on a small stand-in for React's hooks (the runner has no DOM
// and no React renderer): each hook's source is taken whole from its component and given its names, and its state,
// callbacks and effects are kept between renders as React keeps them. M2 audit, area 1 (Harold, 2026-10-21: no
// client-held derived state): after a library change, an answer to a read asked before it is never shown, and a
// failed read shows no old text.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';
import { ApiError } from '../src/api.js';
import { newest, readGuard, unsettled } from '../src/library.js';
import { queryOf } from '../src/search.js';

const source = (name) => readFileSync(fileURLToPath(new URL(`../src/components/${name}`, import.meta.url)), 'utf8');

// The source text of the top-level declarations named, from a component's file.
function declarations(text, names) {
  const found = {};
  const rule = { create: (context) => ({
    'Program > FunctionDeclaration, Program > ExportNamedDeclaration > FunctionDeclaration'(node) {
      if (names.includes(node.id.name)) found[node.id.name] = context.sourceCode.getText(node);
    },
    'Program > VariableDeclaration'(node) {
      for (const d of node.declarations) if (names.includes(d.id.name)) found[d.id.name] = context.sourceCode.getText(d.init);
    },
  }) };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { found: rule } } }, rules: { 'check/found': 'error' } }];
  assert.deepEqual(new Linter().verify(text, config, 'Component.jsx').filter((m) => m.fatal), []);
  return found;
}

// useState, useRef, useCallback and useEffect as React keeps them for one mounted component: slots by call order,
// callbacks kept while their dependencies are the same, an effect run after the render whose dependencies changed (its
// earlier cleanup first). update() renders again while a render or an effect changed state.
function component(hook) {
  const slots = [];
  let index = 0;
  let dirty = false;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    useState(initial) {
      const k = index++;
      if (!(k in slots)) slots[k] = { value: typeof initial === 'function' ? initial() : initial };
      const slot = slots[k];
      return [slot.value, (next) => {
        const value = typeof next === 'function' ? next(slot.value) : next;
        if (!Object.is(value, slot.value)) { slot.value = value; dirty = true; }
      }];
    },
    useRef(initial) {
      const k = index++;
      if (!(k in slots)) slots[k] = { current: initial };
      return slots[k];
    },
    useCallback(fn, deps) {
      const k = index++;
      if (slots[k] && same(slots[k].deps, deps)) return slots[k].fn;
      slots[k] = { fn, deps };
      return fn;
    },
    useEffect(fn, deps) {
      const k = index++;
      if (slots[k] && same(slots[k].deps, deps)) return;
      react.pending.push([k, fn, deps]);
    },
    pending: [],
  };
  let out;
  const make = hook(react);
  return {
    update(...args) {
      if (args.length) this.args = args;
      for (let n = 0; n === 0 || dirty; n += 1) {
        assert.ok(n < 20, 'renders settle');
        dirty = false;
        index = 0;
        react.pending = [];
        out = make(...this.args);
        for (const [k, fn, deps] of react.pending) {
          slots[k]?.cleanup?.();
          slots[k] = { deps, cleanup: fn() };
        }
      }
      return out;
    },
    args: [],
  };
}

// A request the test answers when it chooses: requests[n].answer(value) or .fail(error).
function requests() {
  const asked = [];
  const get = (path) => new Promise((resolve, reject) => asked.push({ path, answer: resolve, fail: reject }));
  return { asked, get };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const listing = (...titles) => ({ materials: titles.map((title, i) => ({ id: `m${i}`, title, state: 'ready' })), asks: [], merging: 0 });
const library = source('Library.jsx');

function useLibraryOf(get, events) {
  const { useLibrary } = declarations(library, ['useLibrary']);
  return component((react) => new Function('useState', 'useCallback', 'useEffect', 'newest', 'readGuard', 'get', 'ApiError',
    'libraryEvents', 'unsettled', 'POLL_MS', 'setTimeout', 'clearTimeout', `return (${useLibrary});`)(
    react.useState, react.useCallback, react.useEffect, newest, readGuard, get, ApiError, events, unsettled, 1500,
    () => 0, () => {}));
}

test('a deletion\'s reload that fails shows no old papers, whichever of its reads is the newest', async () => {
  const { asked, get } = requests();
  const events = new EventTarget();
  const hook = useLibraryOf(get, events);
  hook.update('p1');
  asked[0].answer(listing('A Paper Deleted Next', 'A Paper Kept'));
  await settle();
  assert.equal(hook.update().listing.materials.length, 2);
  // Paper's Delete: the change, then the Library's own reload as it goes back (Library.jsx onDeleted)
  events.dispatchEvent(new CustomEvent('changed', { detail: 'p1' }));
  hook.update().load();
  assert.equal(asked.length, 3);
  for (const request of asked.slice(1)) request.fail(new ApiError(500, 'internal'));
  await settle();
  const after = hook.update();
  assert.equal(after.listing, null);
  assert.equal(after.problem, 'internal');
});

test('an answer to a read asked before a change is never shown, and a read after it is', async () => {
  const { asked, get } = requests();
  const events = new EventTarget();
  const hook = useLibraryOf(get, events);
  hook.update('p1');
  asked[0].answer(listing('A'));
  await settle();
  hook.update().load(); // a poll, asked before the change
  events.dispatchEvent(new CustomEvent('changed', { detail: null }));
  hook.update();
  asked[1].answer(listing('A Paper Deleted Since'));
  await settle();
  assert.deepEqual(hook.update().listing.materials.map((m) => m.title), ['A']);
  asked[2].answer(listing('B'));
  await settle();
  assert.deepEqual(hook.update().listing.materials.map((m) => m.title), ['B']);
  events.dispatchEvent(new CustomEvent('changed', { detail: 'p2' })); // another project's change: no read
  hook.update();
  assert.equal(asked.length, 3);
  hook.update().forget('m0'); // a deleted paper leaves the listing at once
  assert.deepEqual(hook.update().listing.materials, []);
});

test('an @-mention read asked before a library change never restores a deleted paper', async () => {
  const conversation = source('ConversationView.jsx');
  const { useMention, MENTION } = declarations(conversation, ['useMention', 'MENTION']);
  const { asked, get } = requests();
  const events = new EventTarget();
  const hook = component((react) => new Function('useState', 'useEffect', 'get', 'libraryEvents', 'readGuard', 'MENTION',
    `return (${useMention});`)(react.useState, react.useEffect, get, events, readGuard, new Function(`return ${MENTION};`)()));
  assert.equal(hook.update('p1', 'about @').open, false); // reading the project's papers
  assert.equal(asked.length, 1);
  events.dispatchEvent(new CustomEvent('changed', { detail: 'p1' }));
  asked[0].answer(listing('A Paper Deleted Since')); // answered before the change is rendered
  await settle();
  let shown = hook.update();
  assert.equal(shown.open, false); // dropped, and read again
  assert.equal(asked.length, 2);
  asked[1].answer(listing('A Paper Kept'));
  await settle();
  shown = hook.update();
  assert.deepEqual(shown.papers.map((p) => p.title), ['A Paper Kept']);
  events.dispatchEvent(new CustomEvent('changed', { detail: null }));
  assert.equal(hook.update().open, false); // a change drops the papers shown at once
});

test('the Library\'s search drops its results at a change and searches again, never showing an answer asked before', async () => {
  const { useSearch } = declarations(library, ['useSearch']);
  const { asked, get } = requests();
  const events = new EventTarget();
  const action = (react) => () => {
    const [busy, setBusy] = react.useState(false);
    return { busy, problem: null, reset: () => {}, run: async (fn) => { setBusy(true); try { return await fn(); } finally { setBusy(false); } } };
  };
  const hook = component((react) => new Function('useState', 'useRef', 'useEffect', 'newest', 'useAction', 'searchProject',
    'queryOf', 'libraryEvents', `return (${useSearch});`)(react.useState, react.useRef, react.useEffect, newest, action(react),
    (project, query) => get(`${project}:${query}`), queryOf, events));
  hook.update('p1').setText('wages');
  hook.update().search();
  asked[0].answer({ results: [{ passage_id: 'x', title: 'A Paper Deleted Next', text: 'Its passage.' }] });
  await settle();
  assert.equal(hook.update().shown.found.results[0].title, 'A Paper Deleted Next');
  events.dispatchEvent(new CustomEvent('changed', { detail: 'p1' }));
  assert.equal(hook.update().shown, null); // dropped at once
  assert.deepEqual(asked.map((r) => r.path), ['p1:wages', 'p1:wages']); // and searched again
  asked[1].answer({ results: [] });
  await settle();
  assert.deepEqual(hook.update().shown.found.results, []);
});
