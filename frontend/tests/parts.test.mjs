// The parts of the window loaded when first opened (parts.js, components/Parts.jsx): while a part
// loads its fallback shows through Suspense, once it failed to load what failed shows instead, and
// any other error goes on as before; a part loaded early (the Markdown renderer) loads once, is
// waited for without failing what waits, and is drawn at once once loaded. And, in the components'
// source parsed with ESLint's parser: Settings, a paper's page and the Markdown renderer are
// imported only by import() in Parts.jsx, a conversation's read waits for the renderer, every part
// has a fallback and a failure, and the failure is the existing LoadState with Try again.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Suspense } from 'react';
import { Linter } from 'eslint';
import { Boundary, NotLoaded, early, loader } from '../src/parts.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };

const SRC = fileURLToPath(new URL('../src/', import.meta.url));

test('a part\'s loader gives its named export, and a module that failed to load as NotLoaded', async () => {
  assert.deepEqual(await loader(async () => ({ Settings: 'the component' }), 'Settings')(), { default: 'the component' });
  const refused = new TypeError('Failed to fetch dynamically imported module');
  await assert.rejects(loader(() => Promise.reject(refused), 'Settings')(),
    (error) => error instanceof NotLoaded && error.cause === refused && error.message === refused.message);
});

test('the boundary shows the fallback while loading, the failure once not loaded, and passes other errors on', () => {
  const boundary = new Boundary({ fallback: 'loading', failed: 'failed', children: 'the part' });
  const loading = boundary.render();
  assert.equal(loading.type, Suspense);
  assert.deepEqual(loading.props, { fallback: 'loading', children: 'the part' });
  boundary.state = Boundary.getDerivedStateFromError(new NotLoaded('refused'));
  assert.equal(boundary.render(), 'failed');
  const bug = new RangeError('a bug in the part');
  boundary.state = Boundary.getDerivedStateFromError(bug);
  assert.throws(() => boundary.render(), (error) => error === bug);
});

test('a part loaded early loads once, is waited for until it settles, and is drawn at once once loaded', async () => {
  let calls = 0;
  let arrive;
  const markdown = early(() => { calls += 1; return new Promise((resolve) => { arrive = resolve; }); }, 'default');
  let settled = false;
  const ready = markdown.ready().then(() => { settled = true; });
  markdown.ready();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(settled, false); // a conversation's read waits while the renderer loads
  assert.equal(markdown.loaded(), null);
  arrive({ default: 'the renderer' });
  await ready;
  assert.equal(markdown.loaded(), 'the renderer'); // set by the time the read goes on, so answers draw at once
  assert.equal(markdown.failed(), false);
  assert.deepEqual(await loader(markdown.load, 'default')(), { default: 'the renderer' });
  assert.equal(calls, 1);

  const refused = new TypeError('Failed to fetch dynamically imported module');
  const failing = early(() => Promise.reject(refused), 'default');
  assert.equal(await failing.ready(), undefined); // the read goes on: its turns show
  assert.equal(failing.loaded(), null);
  assert.equal(failing.failed(), true); // known by then too, so each answer says so at once
  await assert.rejects(loader(failing.load, 'default')(), (error) => error instanceof NotLoaded && error.cause === refused);
});

// Each module's imports, by path under src (or package name): static ones, re-exports among them,
// and the sources of import() calls.
function imports(text, file) {
  const found = { static: [], dynamic: [] };
  const resolve = (source) => (source.startsWith('.') ? posix.normalize(posix.join(posix.dirname(file), source)) : source);
  const from = (node) => node.source && found.static.push(resolve(node.source.value));
  const rule = { create: () => ({
    ImportDeclaration: from, ExportNamedDeclaration: from, ExportAllDeclaration: from,
    ImportExpression(node) { found.dynamic.push(resolve(node.source.value)); },
  }) };
  const config = [{ files: ['**/*.js', '**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { imports: rule } } }, rules: { 'check/imports': 'error' } }];
  assert.deepEqual(new Linter().verify(text, config, file).filter((m) => m.fatal), []);
  return found;
}

test('Settings, a paper\'s page and the Markdown renderer load only through Parts.jsx\'s import()', () => {
  const LATER = ['components/Settings.jsx', 'components/Paper.jsx', 'markdown.js'];
  const found = Object.fromEntries(readdirSync(SRC, { recursive: true }).filter((name) => /\.jsx?$/.test(name))
    .map((name) => [name, imports(readFileSync(join(SRC, name), 'utf8'), name)]));
  assert.deepEqual(Object.entries(found).flatMap(([name, { static: sources }]) => sources
    .filter((source) => LATER.includes(source) || (source === 'react-markdown' && name !== 'markdown.js'))
    .map((source) => `${name} imports ${source}`)), []);
  assert.deepEqual(Object.entries(found).flatMap(([name, { dynamic }]) => dynamic.map((source) => `${name} ${source}`)),
    LATER.map((source) => `components/Parts.jsx ${source}`));
  assert.ok(found['markdown.js'].static.includes('react-markdown'));
  for (const name of ['Shell.jsx', 'Library.jsx', 'ConversationView.jsx']) {
    assert.ok(found[`components/${name}`].static.includes('components/Parts.jsx'), name);
  }
});

// The arguments of each Promise.all([...]) in the source, as text.
function awaitedTogether(text) {
  const found = [];
  const rule = { create: (context) => ({
    CallExpression(call) {
      if (context.sourceCode.getText(call.callee) === 'Promise.all' && call.arguments[0]?.type === 'ArrayExpression') {
        found.push(call.arguments[0].elements.map((element) => context.sourceCode.getText(element)));
      }
    },
  }) };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { together: rule } } }, rules: { 'check/together': 'error' } }];
  assert.deepEqual(new Linter().verify(text, config, 'Component.jsx').filter((m) => m.fatal), []);
  return found;
}

test('a conversation\'s read waits for the Markdown renderer, so its answers show formatted with their turns', () => {
  const found = awaitedTogether(readFileSync(join(SRC, 'components/ConversationView.jsx'), 'utf8'));
  assert.deepEqual(found, [['get(`/api/conversations/${id}`)', 'markdownReady()']]);
  assert.deepEqual(awaitedTogether('async function load() { setTurns((await get(`/api/conversations/${id}`)).turns); }'), []);
});

test('every part has a fallback and a failure, and the failure offers Try again in both languages', () => {
  const text = readFileSync(join(SRC, 'components/Parts.jsx'), 'utf8');
  const found = { boundaries: [], loadStates: [], statuses: 0 };
  const attributes = (element) => Object.fromEntries(element.attributes.filter((a) => a.type === 'JSXAttribute').map((a) => [a.name.name, a.value]));
  const rule = { create: (context) => ({
    JSXOpeningElement(element) {
      const named = attributes(element);
      if (element.name.name === 'Boundary') found.boundaries.push(Object.keys(named).sort());
      if (element.name.name === 'LoadState') {
        found.loadStates.push({ problem: named.problem?.value, onRetry: context.sourceCode.getText(named.onRetry.expression) });
      }
      if (named.role?.value === 'status') found.statuses += 1;
    },
  }) };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { parts: rule } } }, rules: { 'check/parts': 'error' } }];
  assert.deepEqual(new Linter().verify(text, config, 'Parts.jsx').filter((m) => m.fatal), []);
  assert.deepEqual(found.boundaries, [['failed', 'fallback'], ['failed', 'fallback'], ['failed', 'fallback']]);
  assert.deepEqual(found.loadStates, [{ problem: 'part_not_loaded', onRetry: '() => window.location.reload()' }]);
  assert.equal(found.statuses, 1); // the one loading status every fallback uses
  assert.ok(en['errors.part_not_loaded'] && zhCN['errors.part_not_loaded']);
});
