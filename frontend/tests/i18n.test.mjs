import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { LanguageContext, makeT, resolveLanguage, useT } from '../src/i18n/index.js';

test('t returns the string for the language', () => {
  assert.equal(makeT('en')('sidebar.projects'), 'Projects');
  assert.equal(makeT('zh-CN')('sidebar.projects'), '项目');
});

test('t picks the plural form for the language and formats the count', () => {
  const en = makeT('en');
  assert.equal(en('library.materialCount', { count: 1 }), '1 material');
  assert.equal(en('library.materialCount', { count: 1234 }), '1,234 materials');
  assert.equal(makeT('zh-CN')('library.materialCount', { count: 1 }), '1 份材料');
});

test('t formats date parameters for the language', () => {
  const date = new Date(2026, 9, 2);
  assert.equal(makeT('en')('runs.started', { date }), 'Started 10/2/2026');
  assert.equal(makeT('zh-CN')('runs.started', { date }), '开始于 2026/10/2');
});

test('t substitutes string parameters and leaves a missing one visible', () => {
  const t = makeT('en', { en: { greet: 'Hello {name}, {rest}' } });
  assert.equal(t('greet', { name: 'Ada' }), 'Hello Ada, {rest}');
});

test('a key missing in Chinese falls back to English, with English plural rules', () => {
  const catalogs = {
    en: { only: { one: '{count} item', other: '{count} items' } },
    'zh-CN': {},
  };
  const t = makeT('zh-CN', catalogs);
  assert.equal(t('only', { count: 1 }), '1 item');
  assert.equal(t('only', { count: 2 }), '2 items');
});

test('a key missing in every catalog shows the key', () => {
  assert.equal(makeT('zh-CN')('no.such.key'), 'no.such.key');
});

test('resolveLanguage keeps an explicit language', () => {
  assert.equal(resolveLanguage('en', 'zh-CN'), 'en');
  assert.equal(resolveLanguage('zh-CN', 'en-US'), 'zh-CN');
});

test('resolveLanguage maps any Chinese system locale to zh-CN', () => {
  for (const locale of ['zh', 'zh-CN', 'zh-TW', 'zh-Hans-CN', 'zh-Hant-HK', 'ZH-cn', 'zh_CN.UTF-8']) {
    assert.equal(resolveLanguage('system', locale), 'zh-CN', locale);
  }
});

test('resolveLanguage maps every other system locale to English', () => {
  for (const locale of ['en-US', 'en-GB', 'fr-FR', 'zu', '', undefined]) {
    assert.equal(resolveLanguage('system', locale), 'en', String(locale));
  }
});

test('useT renders in the provided language, English by default', () => {
  const Label = () => useT()('sidebar.conversations');
  assert.equal(renderToStaticMarkup(createElement(Label)), 'Conversations');
  const zh = createElement(LanguageContext.Provider, { value: 'zh-CN' }, createElement(Label));
  assert.equal(renderToStaticMarkup(zh), '对话');
});
