// The parts of the window loaded when first opened (parts.js, components/Parts.jsx): while a part
// loads its fallback shows through Suspense, once it failed to load what failed shows instead, and
// any other error goes on as before. And, in the components' source parsed with ESLint's parser:
// Settings, a paper's page and react-markdown are imported only by import() in Parts.jsx, every
// part has a fallback and a failure, and the failure is the existing LoadState with Try again.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Suspense } from 'react';
import { Linter } from 'eslint';
import { Boundary, NotLoaded, loader } from '../src/parts.js';
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

// Each module's imports: static ones by source, and the sources of import() calls.
function imports(text, file) {
  const found = { static: [], dynamic: [] };
  const rule = { create: () => ({
    ImportDeclaration(node) { found.static.push(node.source.value); },
    ImportExpression(node) { found.dynamic.push(node.source.value); },
  }) };
  const config = [{ files: ['**/*.js', '**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { imports: rule } } }, rules: { 'check/imports': 'error' } }];
  assert.deepEqual(new Linter().verify(text, config, file).filter((m) => m.fatal), []);
  return found;
}

test('Settings, a paper\'s page and react-markdown load only through Parts.jsx\'s import()', () => {
  const LATER = ['./Settings.jsx', './Paper.jsx', 'react-markdown'];
  const found = Object.fromEntries(readdirSync(SRC, { recursive: true }).filter((name) => /\.jsx?$/.test(name))
    .map((name) => [name, imports(readFileSync(join(SRC, name), 'utf8'), name)]));
  assert.deepEqual(Object.entries(found).flatMap(([name, { static: sources }]) =>
    sources.filter((source) => LATER.includes(source)).map((source) => `${name} imports ${source}`)), []);
  assert.deepEqual(Object.entries(found).flatMap(([name, { dynamic }]) => dynamic.map((source) => `${name} ${source}`)),
    LATER.map((source) => `components/Parts.jsx ${source}`));
  for (const name of ['Shell.jsx', 'Library.jsx', 'ConversationView.jsx']) {
    assert.ok(found[`components/${name}`].static.includes('./Parts.jsx'), name);
  }
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
