// The Zotero browser in the interface (S1-15; src/zotero.js, the Add menu, ZoteroBrowser.jsx, a Zotero
// paper's details): the collection tree, the selection's bound, what an import's report and a paper's
// state say, in each supported interface language.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { appendPage, chosenFor, collectionTree, MAX_ITEMS, reportParts, statusKey } from '../src/zotero.js';
import { detailsSource, reasonKey } from '../src/library.js';
import { makeT } from '../src/i18n/index.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zh from '../src/i18n/zh-CN.json' with { type: 'json' };

const source = (name) => readFileSync(fileURLToPath(new URL(`../src/components/${name}`, import.meta.url)), 'utf8');
const date = (value) => value.slice(0, 10);

test('Add offers Zotero, and the browser loads only when first opened', () => {
  const menu = source('AddMenu.jsx');
  assert.match(menu, /t\('library\.addMenu\.zotero'\)/);
  assert.match(menu, /import \{ ZoteroBrowser \} from '\.\/Parts\.jsx'/); // a part (tests/parts.test.mjs)
});

test('collections nest under their parents; an orphan or a cycle is listed at the top, each once', () => {
  const tree = collectionTree([
    { key: 'B', name: 'Beta', parent: null }, { key: 'A', name: 'Alpha', parent: null },
    { key: 'C', name: 'Child', parent: 'A' }, { key: 'D', name: 'Deeper', parent: 'C' },
    { key: 'O', name: 'Orphan', parent: 'GONE' }, { key: 'X', name: 'Ex', parent: 'Y' }, { key: 'Y', name: 'Why', parent: 'X' },
  ]);
  assert.deepEqual(tree.map((c) => [c.key, c.depth]),
    [['A', 0], ['C', 1], ['D', 2], ['B', 0], ['X', 0], ['O', 0], ['Y', 0]]);
  assert.equal(new Set(tree.map((c) => c.key)).size, 7);
});

test('the status and the report are said in each supported interface language', () => {
  const summary = { imported: 2, updated: 1, versions: 1, unchanged: 0, waiting: 1, already: 0, not_found: 1,
    no_title: 0, pdf: { pdf_not_local: 1, pdf_missing: 1 }, left_out: 1 };
  assert.deepEqual(reportParts(summary).map(([key]) => key), ['zotero.report.imported', 'zotero.report.updated',
    'zotero.report.versions', 'zotero.report.waiting', 'zotero.report.pdfNotRead', 'zotero.report.notFound',
    'references.leftOut']);
  assert.deepEqual(reportParts(null), []);
  assert.deepEqual(reportParts({ reason: 'zotero_not_running' }), [['zotero.report.kept', {}]]); // no counts it does not hold
  for (const catalog of [en, zh]) {
    const t = makeT(catalog);
    for (const state of ['checking', 'ready', 'off', 'not_running', 'api_off', 'not_yours', 'unsupported', 'error']) {
      const key = statusKey(state === 'checking' ? null : { state });
      assert.notEqual(t(key), key, key);
    }
    for (const [key, params] of reportParts(summary)) assert.doesNotMatch(t(key, params), /\{|zotero\./, key);
    assert.match(t('zotero.tooMany', { max: MAX_ITEMS }), /100/);
  }
});

test('a Zotero paper says where its details came from and why its PDF was not read', () => {
  for (const catalog of [en, zh]) {
    const t = makeT(catalog);
    const paper = { source: 'zotero', source_key: 'users/0/items/ABCD2345', lookup: null };
    assert.equal(detailsSource(t, paper, {}, date), t('library.source.fromZotero'));
    assert.equal(detailsSource(t, paper, { review_lock: true }, date), t('library.source.zoteroLocked'));
    const completed = { ...paper, resolved_at: '2026-10-15T00:00:00Z', lookup: { status: 'succeeded', source: 'crossref' } };
    assert.equal(detailsSource(t, completed, {}, date),
      t('library.source.zoteroCompletedFrom', { source: t('ask.service.crossref'), date: '2026-10-15' }));
    const missed = { ...paper, lookup: { status: 'failed', outcome: 'unavailable' } };
    assert.equal(detailsSource(t, missed, {}, date), t('library.source.zotero.unavailable'));
    for (const reason of ['pdf_missing', 'pdf_not_local', 'pdf_on_network', 'pdf_not_downloaded', 'pdf_not_a_file',
      'pdf_too_large', 'pdf_not_a_pdf', 'pdf_unreadable', 'pdf_link']) {
      assert.equal(reasonKey(reason), `library.reason.${reason}`);
      assert.notEqual(t(reasonKey(reason)), reasonKey(reason));
    }
  }
});

test('the browser says what an import did only from its finished run', () => {
  const browser = source('ZoteroBrowser.jsx');
  assert.match(browser, /const row = await followRun\(runId/);
  assert.match(browser, /reportParts\(row\.result \?\?/);
});

test('a further page joins only the list it was asked for', () => {
  const list = (library, collection, generation, keys, next) => ({ library, collection, generation, next,
    items: keys.map((key) => ({ key })), total: 120 });
  const a = list('users/0', null, 1, ['A1'], 50);
  const page = { items: [{ key: 'A2' }], next: 100, total: 120 };
  const asked = { library: 'users/0', collection: null, generation: 1, start: 50 };
  assert.deepEqual(appendPage(a, page, asked).items.map((i) => i.key), ['A1', 'A2']);
  const b = list('groups/4711', null, 2, ['B1'], 50); // another library shown since, also at 50
  assert.equal(appendPage(b, page, asked), b);
  assert.equal(appendPage(list('users/0', 'COL00001', 1, ['C1'], 50), page, asked).items.length, 1);
  assert.equal(appendPage(list('users/0', null, 3, ['A1'], 50), page, asked).items.length, 1); // the same list asked again
  const added = appendPage(a, page, asked);
  assert.equal(appendPage(added, page, asked), added); // added once
  assert.equal(appendPage(null, page, asked), null);
});

test('the Local only question for a Zotero import names both identifiers it may send', () => {
  assert.match(en['ask.identifier_lookup.bodyZotero'], /DOI or arXiv ID/);
  assert.match(zh['ask.identifier_lookup.bodyZotero'], /DOI 或 arXiv 编号/);
});

test('a PDF not read again is reported as kept, never as details only', () => {
  const parts = reportParts({ imported: 0, updated: 0, unchanged: 1, pdf: {}, pdf_kept: { pdf_missing: 1 } });
  assert.deepEqual(parts.map(([key]) => key), ['zotero.report.imported', 'zotero.report.unchanged', 'zotero.report.pdfKept']);
  for (const catalog of [en, zh]) assert.doesNotMatch(makeT(catalog)('zotero.report.pdfKept', { count: 1 }), /zotero\.|\{/);
});

test('items chosen under one Zotero are not sent to another', () => {
  assert.deepEqual(chosenFor(['AAAA2222'], 'server-a', 'server-a'), ['AAAA2222']);
  assert.deepEqual(chosenFor(['AAAA2222'], 'server-a', 'server-b'), []);
  assert.deepEqual(chosenFor(['AAAA2222'], null, undefined), ['AAAA2222']); // a Zotero sending no id, both times
  assert.deepEqual(chosenFor(['AAAA2222'], null, 'server-b'), []);
  assert.match(source('ZoteroBrowser.jsx'), /setChosen\(\(keys\) => chosenFor\(keys, under, status\.server_id\)\)/);
});
