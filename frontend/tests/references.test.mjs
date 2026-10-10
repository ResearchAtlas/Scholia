// Reference files in the interface (S1-14; src/references.js, src/library.js, the Add menu, the merge
// question): what the import sends and says, a reference file's paper's details and labels, and a merge
// question's two papers, in each supported interface language.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { importReferences, MAX_REFERENCE_BYTES, paperLine, REFERENCE_ACCEPT, reportParts } from '../src/references.js';
import { detailsSource, latestLookup } from '../src/library.js';
import { makeT } from '../src/i18n/index.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zh from '../src/i18n/zh-CN.json' with { type: 'json' };

const source = (name) => readFileSync(fileURLToPath(new URL(`../src/components/${name}`, import.meta.url)), 'utf8');
const date = (value) => value.slice(0, 10);

test('Add offers files and a reference file, the chooser taking the reference formats', () => {
  const menu = source('AddMenu.jsx');
  assert.match(menu, /t\('library\.addMenu\.files'\)/);
  assert.match(menu, /t\('library\.addMenu\.referenceFile'\)/);
  assert.match(source('Library.jsx'), /<AddMenu project=\{project\} onFiles=\{\(\) => input\.current\?\.click\(\)\}/);
  for (const extension of ['.ris', '.bib', '.json', '.enw', '.txt']) assert.ok(REFERENCE_ACCEPT.split(',').includes(extension));
  for (const key of ['library.add', 'library.addMenu.files', 'library.addMenu.referenceFile', 'references.importing']) {
    assert.ok(key in en && key in zh, key);
  }
});

test('a reference file larger than the backend reads is refused before anything is sent', async () => {
  await assert.rejects(importReferences('p', { size: MAX_REFERENCE_BYTES + 1, name: 'big.ris' }),
    (error) => error.code === 'reference_file_too_large');
  for (const code of ['reference_file_too_large', 'unsupported_reference_file', 'too_many_records', 'no_records',
    'unreadable_file', 'memory_limit', 'step_limit']) {
    assert.ok(`errors.${code}` in en && `errors.${code}` in zh, code);
  }
});

test('the report says what the import did, counted, in each supported interface language', () => {
  const report = { imported: ['a', 'b'], waiting: [{ run_id: 'r', material_id: 'a' }], already: ['c'],
    skipped: { no_title: 1, unreadable: 2 } };
  const en_ = makeT('en');
  assert.deepEqual(reportParts(report).map(([key, params]) => en_(key, params)), [
    'Imported 2 papers.', '1 duplicate waits for your answer.', '1 was in the project already, with nothing to add.',
    '3 records were skipped: they had no title or could not be read.']);
  assert.deepEqual(reportParts({ imported: [], waiting: [], already: [], skipped: {} }).map(([key, params]) => en_(key, params)),
    ['Imported 0 papers.']);
  const zh_ = makeT('zh-CN');
  assert.equal(zh_(...reportParts(report)[0]), '已导入 2 篇文献。');
});

test('a reference file\'s paper says where its details come from: its record, completed by a lookup, or the lookup\'s outcome', () => {
  const t = makeT('en');
  const paper = { source: 'reference_file', checked_by: null };
  assert.equal(detailsSource(t, paper, {}, date), 'From the reference file');
  assert.equal(detailsSource(t, paper, { review_lock: true }, date), en['library.source.referenceLocked']);
  const completed = { ...paper, source_key: 'openalex:W1', resolved_at: '2026-01-02T00:00:00.000Z',
    lookup: { status: 'succeeded', outcome: 'resolved' } };
  assert.equal(detailsSource(t, completed, {}, date), 'From the reference file, completed from OpenAlex, 2026-01-02');
  for (const outcome of ['not_found', 'unavailable', 'refused']) {
    assert.equal(detailsSource(t, { ...paper, lookup: { status: 'succeeded', outcome } }, {}, date),
      en[`library.source.reference.${outcome}`]);
    assert.ok(`library.source.reference.${outcome}` in zh);
  }
  assert.equal(detailsSource(t, { ...paper, lookup: { status: 'running', waiting: true } }, {}, date), en['library.source.waiting']);
  assert.equal(latestLookup(t, { ...paper, lookup: { status: 'failed', outcome: null, reason: 'unavailable' } }),
    en['errors.unavailable']);
  // Edited by the researcher: theirs, as for any paper.
  assert.equal(detailsSource(t, { ...paper, checked_by: 'researcher', checked_at: '2026-03-04T00:00:00.000Z' }, {}, date),
    'Edited by you, 2026-03-04');
});

test('a paper with no file says it has no full text and no file, and offers Attach', () => {
  const library = source('Library.jsx');
  assert.match(library, /material\.evidence_type === 'full_text'\) return null/);
  assert.match(library, /material\.version\.media_type \? .* : t\('library\.noFile'\)\)/s);
  assert.match(source('Paper.jsx'), /t\(replacing \? 'paper\.attaching' : 'paper\.attach'\)/);
  for (const key of ['library.noFullText', 'library.noFile', 'paper.attach', 'paper.attaching']) assert.ok(key in en && key in zh, key);
});

test('a merge question shows both papers, what matched, and its options in each supported interface language', () => {
  const t = makeT('en');
  const paper = { title: 'A Paper', authors: ['Kept, Author', 'Two', 'Three'], author_count: 5, year: 2020,
    venue: 'Journal', doi: '10.5555/x' };
  assert.equal(paperLine(t, paper), 'Kept, Author, Two, Three et al. · 2020 · Journal · DOI 10.5555/x');
  assert.equal(paperLine(t, { title: 'Bare', authors: [], author_count: 0, year: null, venue: null, doi: null }), '');
  const ask = source('Ask.jsx');
  assert.match(ask, /ask\.kind === 'merge' && <MergeComparison ask=\{ask\} \/>/);
  assert.match(ask, /line-clamp-2 break-words font-medium" title=\{item\.title\}/); // a long title, whole on hover
  for (const key of ['ask.merge.question', 'ask.merge.body', 'ask.merge.option.merge', 'ask.merge.option.keep_both',
    'ask.merge.paper', 'ask.merge.record', 'ask.merge.noDetails', 'ask.merge.matched.doi', 'ask.merge.matched.title',
    'ask.kindName.merge', 'ask.answer.merge', 'ask.answer.keep_both', 'settings.workflowMerge', 'references.doi']) {
    assert.ok(key in en && key in zh, key);
  }
});

test('a Local only lookup of a reference file\'s papers says its answer covers those papers', () => {
  assert.match(source('Ask.jsx'), /ask\.params\?\.batch === 'reference_file' \? 'ReferenceFile' : ''/);
  assert.match(en['ask.identifier_lookup.bodyReferenceFile'], /the papers this reference file added/);
  assert.match(zh['ask.identifier_lookup.bodyReferenceFile'], /这个参考文献文件添加的文献/);
  assert.doesNotMatch(en['ask.identifier_lookup.bodyReferenceFile'], /files you just added/);
});
