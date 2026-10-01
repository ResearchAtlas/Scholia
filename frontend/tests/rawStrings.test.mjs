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

const LETTER = '/\\p{L}/u';
const TEXT = `:matches(Literal[value=${LETTER}], TemplateElement[value.raw=${LETTER}])`;
const ATTRIBUTE = 'JSXAttribute[name.name=/^(title|placeholder|alt|label|aria-label)$/]';
// Where a string is shown: a JSX child, or a visible attribute's value. Either
// branch of ?:, && and || counts; arguments of calls such as t('key') do not.
const SHOWN = [':matches(JSXElement, JSXFragment) > JSXExpressionContainer', ATTRIBUTE, `${ATTRIBUTE} > JSXExpressionContainer`];
const SELECTORS = [
  `JSXText[value=${LETTER}]`,
  ...SHOWN.flatMap((shown) => [
    `${shown} > ${TEXT}`,
    `${shown} > TemplateLiteral > ${TEXT}`,
    `${shown} > :matches(ConditionalExpression, LogicalExpression) > ${TEXT}`,
    `${shown} > :matches(ConditionalExpression, LogicalExpression) > TemplateLiteral > ${TEXT}`,
  ]),
];

const CONFIG = [{
  files: ['**/*.jsx'],
  languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
  rules: {
    'no-restricted-syntax': ['error', ...SELECTORS.map((selector) => ({ selector, message: 'raw UI string' }))],
  },
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
  ]);
});

test('the raw-string check accepts text passed through t()', () => {
  assert.deepEqual(rawStrings(join(FIXTURES, 'clean.jsx')), []);
});
