// A migrated component, marked by a "// i18n: migrated" line, must show no raw
// UI strings: its visible text goes through t(). Parsed with ESLint's parser.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { basename, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));
const FIXTURES = fileURLToPath(new URL('./fixtures/', import.meta.url));
const MARKER = /^\/\/ i18n: migrated$/m;

const LETTER = /\p{L}/u;
const VISIBLE_ATTRIBUTES = new Set(['title', 'placeholder', 'alt', 'label', 'aria-label']);

// Reports the string text an expression can render: a string or template
// literal, every branch of ?:, && and || however deeply nested, the last
// expression of a sequence, and expressions inside a template literal.
// Arguments of calls such as t('key') are not rendered, so they are not walked.
function reportRendered(context, node) {
  switch (node.type) {
    case 'Literal':
      if (typeof node.value === 'string' && LETTER.test(node.value)) context.report({ node, message: 'raw UI string' });
      break;
    case 'TemplateLiteral':
      if (node.quasis.some((quasi) => LETTER.test(quasi.value.raw))) context.report({ node, message: 'raw UI string' });
      node.expressions.forEach((expression) => reportRendered(context, expression));
      break;
    case 'ConditionalExpression':
      reportRendered(context, node.consequent);
      reportRendered(context, node.alternate);
      break;
    case 'LogicalExpression':
      reportRendered(context, node.left);
      reportRendered(context, node.right);
      break;
    case 'SequenceExpression':
      reportRendered(context, node.expressions.at(-1));
      break;
  }
}

const isVisibleAttribute = (node) => node.type === 'JSXAttribute' && VISIBLE_ATTRIBUTES.has(node.name.name);

// Where text is shown: JSX text, a JSX child expression, or a visible attribute.
const noRawStrings = {
  create: (context) => ({
    JSXText(node) {
      if (LETTER.test(node.value)) context.report({ node, message: 'raw UI string' });
    },
    JSXExpressionContainer(node) {
      if (['JSXElement', 'JSXFragment'].includes(node.parent.type) || isVisibleAttribute(node.parent)) {
        reportRendered(context, node.expression);
      }
    },
    JSXAttribute(node) {
      if (isVisibleAttribute(node) && node.value?.type === 'Literal') reportRendered(context, node.value);
    },
  }),
};

const CONFIG = [{
  files: ['**/*.jsx'],
  languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
  plugins: { i18n: { rules: { 'no-raw-strings': noRawStrings } } },
  rules: { 'i18n/no-raw-strings': 'error' },
}];

// Each problem as "file:line:column message"; a parse error is reported too.
function rawStrings(file) {
  const messages = new Linter().verify(readFileSync(file, 'utf8'), CONFIG, file);
  return messages.map((m) => `${basename(file)}:${m.line}:${m.column} ${m.message}`);
}

function migratedComponents(dir) {
  return readdirSync(dir, { recursive: true })
    .filter((name) => name.endsWith('.jsx'))
    .map((name) => join(dir, name))
    .filter((file) => MARKER.test(readFileSync(file, 'utf8')))
    .sort();
}

test('migrated components contain no raw UI strings', () => {
  assert.deepEqual(migratedComponents(SRC).flatMap(rawStrings), []);
});

test('only components marked as migrated are checked', () => {
  assert.deepEqual(migratedComponents(FIXTURES), [join(FIXTURES, 'clean.jsx'), join(FIXTURES, 'raw-strings.jsx')]);
});

test('the raw-string check flags each kind of raw UI string', () => {
  assert.deepEqual(rawStrings(join(FIXTURES, 'raw-strings.jsx')).map((p) => p.split(' ')[0]), [
    'raw-strings.jsx:5:39', // title="Raw title"
    'raw-strings.jsx:5:51', // JSX text
    'raw-strings.jsx:7:8', // {'Raw child'}
    'raw-strings.jsx:8:15', // ?: branch
    'raw-strings.jsx:9:23', // && fallback
    'raw-strings.jsx:10:8', // template literal child
    'raw-strings.jsx:11:27', // placeholder={'...'}
    'raw-strings.jsx:11:74', // aria-label branch
    'raw-strings.jsx:12:17', // alt={`...`}
    'raw-strings.jsx:13:9', // Chinese JSX text
    'raw-strings.jsx:14:16', // && template literal
    'raw-strings.jsx:15:27', // ?: nested in &&
    'raw-strings.jsx:16:35', // || nested in ?: nested in ?:
    'raw-strings.jsx:17:16', // last expression of a sequence
    'raw-strings.jsx:18:18', // ?: inside a template literal
  ]);
});

test('the raw-string check accepts text passed through t()', () => {
  assert.deepEqual(rawStrings(join(FIXTURES, 'clean.jsx')), []);
});
