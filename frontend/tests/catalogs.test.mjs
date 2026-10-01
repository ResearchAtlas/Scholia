// Both catalogs must have the same keys, and each key the same placeholders.
import test from 'node:test';
import assert from 'node:assert/strict';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };

// A plural entry's placeholders are those of all its forms together.
function placeholders(value) {
  const texts = typeof value === 'string' ? [value] : Object.values(value);
  return [...new Set(texts.flatMap((text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1])))].sort();
}

function catalogProblems(catalogs) {
  const problems = [];
  const [[baseName, base], ...others] = Object.entries(catalogs);
  for (const [name, catalog] of Object.entries(catalogs)) {
    for (const [key, value] of Object.entries(catalog)) {
      const ok = typeof value === 'string' ||
        (value && typeof value === 'object' && typeof value.other === 'string' &&
          Object.values(value).every((form) => typeof form === 'string'));
      if (!ok) problems.push(`${name}: ${key} is not a string or plural forms with "other"`);
    }
  }
  for (const [name, catalog] of others) {
    for (const key of Object.keys(base)) {
      if (!Object.hasOwn(catalog, key)) problems.push(`${name}: missing ${key}`);
    }
    for (const key of Object.keys(catalog)) {
      if (!Object.hasOwn(base, key)) {
        problems.push(`${name}: ${key} is not in ${baseName}`);
      } else if (placeholders(base[key]).join() !== placeholders(catalog[key]).join()) {
        problems.push(`${name}: ${key} placeholders differ from ${baseName}`);
      }
    }
  }
  return problems;
}

test('the en and zh-CN catalogs have the same keys and placeholders', () => {
  assert.deepEqual(catalogProblems({ en, 'zh-CN': zhCN }), []);
});

test('the catalog check fails on missing keys, extra keys and placeholder mismatches', () => {
  const problems = catalogProblems({
    en: { a: 'A', b: 'B {x}', c: { one: '{count} c', other: '{count} cs' } },
    'zh-CN': { b: 'B {y}', c: { other: '{count} 个' }, d: 'D', e: { one: 'no other form' } },
  });
  assert.deepEqual(problems, [
    'zh-CN: e is not a string or plural forms with "other"',
    'zh-CN: missing a',
    'zh-CN: b placeholders differ from en',
    'zh-CN: d is not in en',
    'zh-CN: e is not in en',
  ]);
});
