// The papers' read (src/libraryRead.js) and the hooks that read through it (the Library's listing and search, the message
// box's @-mentions) run as written, on a small stand-in for React's hooks (the runner has no DOM and no React renderer):
// each hook's source is taken whole from its file and given its names, and its state, callbacks and effects are kept
// between renders as React keeps them. M2 audit, area 1 (Harold, 2026-10-21: no client-held derived state; design
// review F, option 2.2, pending Harold): after a change to the papers nothing read before it is shown, the view shows
// its loading state until a read asked after it answers, and a failed read shows no old text. The delete dialog fires
// its change when its request settles (rider 1), drawn with the real React through Vite as tests/memory.test.mjs does.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';
import { ApiError } from '../src/api.js';
import { libraryChanged, libraryGeneration, unsettled, watchLibrary } from '../src/library.js';
import { queryOf } from '../src/search.js';

const file = (name) => readFileSync(fileURLToPath(new URL(`../src/${name}`, import.meta.url)), 'utf8');

// The source text of the top-level declarations named, from a source file.
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

// useState, useRef, useCallback, useEffect and useSyncExternalStore as React keeps them for one mounted component: slots
// by call order, callbacks kept while their dependencies are the same, an effect run after the render whose dependencies
// changed (its earlier cleanup first), a store's snapshot read at each render. update() renders again while a render or
// an effect changed state; a test calls it where React would render (after a change, after an answer).
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
    useSyncExternalStore: (subscribe, snapshot) => { index++; return snapshot(); },
    pending: [],
  };
  let out;
  const make = hook(react);
  return {
    args: [],
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
  };
}

// libraryRead.js's hook, given the stand-in's hooks and the real papers' generation.
function readHook(react) {
  const { useLibraryRead } = declarations(file('libraryRead.js'), ['useLibraryRead']);
  return new Function('useCallback', 'useEffect', 'useRef', 'useState', 'useSyncExternalStore', 'libraryGeneration',
    'watchLibrary', `return (${useLibraryRead});`)(react.useCallback, react.useEffect, react.useRef, react.useState,
    react.useSyncExternalStore, libraryGeneration, watchLibrary);
}

// A request the test answers when it chooses: asked[n].answer(value) or .fail(error).
function requests() {
  const asked = [];
  const get = (path) => new Promise((resolve, reject) => asked.push({ path, answer: resolve, fail: reject }));
  return { asked, get };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const listing = (...titles) => ({ materials: titles.map((title, i) => ({ id: `m${i}`, title, state: 'ready' })),
  asks: titles.length ? [{ ask_id: 'a1', params: { paper: { title: titles[0] } } }] : [], merging: 0 });
const library = file('components/Library.jsx');
const conversation = file('components/ConversationView.jsx');

function useLibraryOf(get) {
  const { useLibrary } = declarations(library, ['useLibrary']);
  return component((react) => new Function('useEffect', 'useLibraryRead', 'get', 'ApiError', 'unsettled', 'POLL_MS',
    'setTimeout', 'clearTimeout', `return (${useLibrary});`)(react.useEffect, readHook(react), get, ApiError, unsettled,
    1500, () => 0, () => {}));
}

function useSearchOf(search) {
  const { useSearch } = declarations(library, ['useSearch']);
  return component((react) => new Function('useT', 'useContext', 'LanguageContext', 'useState', 'useEffect',
    'useLibraryRead', 'queryOf', 'searchProject', 'problemText', `return (${useSearch});`)(() => (key) => key, () => 'en',
    null, react.useState, react.useEffect, readHook(react), queryOf, search, (t, error) => error.code));
}

function useMentionOf(get) {
  const { useMention, MENTION } = declarations(conversation, ['useMention', 'MENTION']);
  return component((react) => new Function('useState', 'useLibraryRead', 'get', 'MENTION', `return (${useMention});`)(
    react.useState, readHook(react), get, new Function(`return ${MENTION};`)()));
}

test('recheck 1: a deletion\'s change, then the Library\'s own reload: the first answers, the second fails, and no old papers are shown', async () => {
  const { asked, get } = requests();
  const hook = useLibraryOf(get);
  hook.update('p1');
  asked[0].answer(listing('A Paper Deleted Next', 'A Paper Kept'));
  await settle();
  assert.equal(hook.update().listing.materials.length, 2);
  libraryChanged('p1');
  hook.update().load(); // the Library's own reload as it goes back
  assert.equal(asked.length, 3);
  asked[1].answer(listing('A Paper Kept'));
  asked[2].fail(new ApiError(500, 'internal'));
  await settle();
  const after = hook.update();
  assert.equal(after.listing, null);
  assert.equal(after.problem, 'internal');
});

test('round 2: nothing of the listing, its questions included, is shown from the change until a read after it answers', async () => {
  const { asked, get } = requests();
  const hook = useLibraryOf(get);
  hook.update('p1');
  asked[0].answer(listing('A Paper With a Merge Waiting'));
  await settle();
  assert.equal(hook.update().listing.asks.length, 1);
  hook.update().load(); // a poll, asked before the change and answered after it
  libraryChanged(null);
  assert.equal(hook.update().listing, null); // at once, before any answer: the loading line
  asked[1].answer(listing('A Paper Deleted Since'));
  await settle();
  assert.equal(hook.update().listing, null);
  asked[2].answer(listing('B'));
  await settle();
  assert.deepEqual(hook.update().listing.materials.map((m) => m.title), ['B']);
});

test('a poll that fails with no change keeps the listing and adds its error; another project shows nothing of the first', async () => {
  const { asked, get } = requests();
  const hook = useLibraryOf(get);
  hook.update('p1');
  asked[0].answer(listing('A'));
  await settle();
  hook.update().load();
  asked[1].fail(new ApiError(503, 'unreachable'));
  await settle();
  let shown = hook.update();
  assert.deepEqual([shown.listing.materials.map((m) => m.title), shown.problem], [['A'], 'unreachable']);
  shown = hook.update('p2');
  assert.equal(shown.listing, null);
  assert.equal(asked.at(-1).path, '/api/projects/p2/materials');
  asked[1 + 1].answer(listing('Of Another Project'));
  await settle();
  assert.deepEqual(hook.update().listing.materials.map((m) => m.title), ['Of Another Project']);
});

test('recheck 1: an @-mention read asked before a change, answered before the change is drawn, never offers a deleted paper', async () => {
  const { asked, get } = requests();
  const hook = useMentionOf(get);
  assert.equal(hook.update('p1', 'about @').open, false); // reading the project's papers
  assert.equal(asked.length, 1);
  libraryChanged('p1');
  asked[0].answer(listing('A Paper Deleted Since')); // answered before the change is rendered
  await settle();
  let shown = hook.update();
  assert.equal(shown.open, false); // dropped, and read again
  assert.equal(asked.length, 2);
  asked[1].answer(listing('A Paper Kept'));
  await settle();
  shown = hook.update();
  assert.deepEqual(shown.papers.map((p) => p.title), ['A Paper Kept']);
  libraryChanged(null);
  assert.equal(hook.update().open, false); // a change drops the papers shown at once
});

test('audit astra F1: a search asked before a deletion and answered after it is never shown; the search is asked again', async () => {
  const { asked, get } = requests();
  const hook = useSearchOf((project, query) => get(`${project}:${query}`));
  hook.update('p1').setText('wages');
  hook.update().search();
  hook.update();
  asked[0].answer({ results: [{ passage_id: 'x', title: 'A Paper Deleted Next', text: 'Its passage.' }] });
  await settle();
  assert.equal(hook.update().shown.found.results[0].title, 'A Paper Deleted Next');
  hook.update().search(); // the same query asked again, then the paper deleted before it answers
  hook.update();
  libraryChanged('p1');
  assert.equal(hook.update().shown, null); // dropped at once
  asked[1].answer({ results: [{ passage_id: 'x', title: 'A Paper Deleted Next', text: 'Its passage.' }] });
  await settle();
  assert.equal(hook.update().shown, null);
  assert.deepEqual(asked.map((r) => r.path), ['p1:wages', 'p1:wages', 'p1:wages']);
  asked[2].answer({ results: [] });
  await settle();
  assert.deepEqual(hook.update().shown.found.results, []);
});

test('round 2: an obsolete search\'s failure never overwrites a fresh search\'s results, error or busy state', async () => {
  const { asked, get } = requests();
  const hook = useSearchOf((project, query) => get(`${project}:${query}`));
  hook.update('p1').setText('wages');
  hook.update().search();
  hook.update();
  libraryChanged('p1');
  assert.equal(hook.update().busy, true);
  asked[1].answer({ results: [{ passage_id: 'y', title: 'Fresh', text: 'A fresh passage.' }] });
  await settle();
  asked[0].fail(new ApiError(500, 'internal'));
  await settle();
  const shown = hook.update();
  assert.deepEqual([shown.shown.found.results[0].title, shown.problem, shown.busy], ['Fresh', null, false]);
});

// Source checks (design review F's test 11 and test 10): the papers and the search are read nowhere else in the Library
// and the message box, and every action of the Library that changes papers tells every view.
test('the Library and the @-mentions read the papers only through useLibraryRead, and the Library\'s Add is a change', () => {
  const reads = (text) => [...text.matchAll(/\/materials`\)/g)].length;
  assert.equal(reads(library), 1);
  assert.match(library, /useLibraryRead\(projectId \|\| null, \(\) => get\(`\/api\/projects\/\$\{encodeURIComponent\(projectId\)\}\/materials`\)\)/);
  assert.equal([...library.matchAll(/searchProject\(/g)].length, 1);
  assert.match(library, /useLibraryRead\([^;]*\(\) => searchProject\(projectId, query\)\)/);
  const mention = conversation.slice(conversation.indexOf('function useMention('));
  assert.equal(reads(mention), 1);
  assert.match(mention, /useLibraryRead\(typed \? projectId : null, \(\) => get\(/);
  assert.doesNotMatch(library, /readGuard|forget/);
  assert.match(library, /if \(result\) libraryChanged\(project\.id\);/); // Add
});

test('design review F\'s rider 1: the delete dialog fires its change when its request settles, with a notice or failing', async () => {
  const { createServer } = await import('vite');
  const { createElement: h } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true, ws: false, watch: null }, optimizeDeps: { noDiscovery: true } });
  const answer = globalThis.fetch;
  try {
    const { DeleteDialog } = await server.ssrLoadModule('/src/components/DeleteDialog.jsx');
    const press = async (reply) => { // the dialog's Delete, drawn once; its state after that is a no-op
      const heard = [];
      let tree;
      renderToStaticMarkup(h(function Drawn() {
        tree = DeleteDialog({ target: { kind: 'material', id: 'm1', title: 'Delete it?', body: '' },
          onClose: () => heard.push('closed'), onDone: () => heard.push('done'), onSettled: () => heard.push('settled') });
        return null;
      }));
      const buttons = [];
      const walk = (node) => {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) { node.forEach(walk); return; }
        if (node.props?.onClick && node.props.variant === 'destructive') buttons.push(node);
        walk(node.props?.children);
      };
      walk(tree);
      globalThis.fetch = reply;
      await buttons[0].props.onClick();
      return heard;
    };
    const json = (status, body) => async () => new Response(JSON.stringify(body), { status });
    assert.deepEqual(await press(json(200, { ok: true })), ['settled', 'done']);
    assert.deepEqual(await press(json(200, { ok: true, purge_failed: true })), ['settled']); // its notice is read first
    assert.deepEqual(await press(json(500, { code: 'internal' })), ['settled']); // the dialog keeps its error
    assert.deepEqual(await press(async () => { throw new TypeError('no answer'); }), ['settled']);
  } finally {
    globalThis.fetch = answer;
    await server.close();
  }
});
