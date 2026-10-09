// Search as the interface reads it (src/search.js; S1-17): the field's keys, input methods included,
// blank input, what results say of how they were found, where a passage is, a paper's index line,
// the offer's outcome, and the catalog entries each names.
import test from 'node:test';
import assert from 'node:assert/strict';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };
import { makeT } from '../src/i18n/index.js';
import { fieldKey, indexReason, offerOutcome, paperIndexed, queryOf, searchNote, whereIs } from '../src/search.js';

const t = makeT('en');

test('Enter searches, Escape clears, and Enter while an input method composes does neither', () => {
  assert.equal(fieldKey({ key: 'Enter', isComposing: false, keyCode: 13 }), 'search');
  assert.equal(fieldKey({ key: 'Enter', isComposing: true, keyCode: 13 }), null); // choosing 最低工资 in pinyin
  assert.equal(fieldKey({ key: 'Enter', isComposing: false, keyCode: 229 }), null); // WebKit's composing Enter
  assert.equal(fieldKey({ key: 'Escape', isComposing: false, keyCode: 27 }), 'clear');
  assert.equal(fieldKey({ key: 'a', isComposing: false, keyCode: 65 }), null);
});

test('blank or invisible-only input searches nothing', () => {
  for (const text of ['', '   ', '​⁠', '　']) assert.equal(queryOf(text), null);
  assert.equal(queryOf('  最低工资 '), '最低工资');
});

test('results say when search is keyword-only and why, the index is rebuilt, or meaning search covers part', () => {
  const note = (found, offered) => {
    const said = searchNote(found, offered);
    return said && t(said[0], said[1].reasonKey ? { reason: t(said[1].reasonKey) } : said[1]);
  };
  assert.equal(note({ mode: 'hybrid', reason: null, index: 'ready', coverage: { embedded: 4, total: 4 } }), null);
  assert.equal(note({ mode: 'hybrid', reason: null, index: 'ready', coverage: { embedded: 3, total: 10 } }),
    'Meaning search covers 3 of 10 passages so far; the others are found by keywords.');
  assert.equal(note({ mode: 'hybrid', index: 'building', coverage: { embedded: 0, total: 0 } }), t('search.building'));
  assert.match(note({ mode: 'keyword_only', reason: 'deadline' }), /^Search is keyword-only: meaning search took too long/);
  assert.match(note({ mode: 'keyword_only', reason: 'model_missing' }), /Download it, or import its file/);
  assert.doesNotMatch(note({ mode: 'keyword_only', reason: 'model_missing' }, false), /Download/); // Local only: import only
  assert.equal(note({ mode: 'keyword_only', reason: 'something new' }), t('helper.keywordOnly', { reason: t('helper.reason.other') }));
  for (const reason of ['deadline', 'vectors_unavailable', 'index_unavailable', 'request_failed', 'closing']) {
    for (const catalog of [en, zhCN]) assert.ok(`search.reason.${reason}` in catalog, reason);
  }
  assert.equal(searchNote(null), null);
});

test('a result says its page and section path', () => {
  assert.equal(whereIs(t, { page: 3, section_path: ['Methods', 'Data'] }), 'p. 3 · Methods › Data');
  assert.equal(whereIs(t, { page: null, section_path: ['研究发现'] }), '研究发现');
  assert.equal(whereIs(t, { page: null, section_path: [] }), '');
});

test("a paper's index line under Details", () => {
  const index = { mode: 'hybrid', materials: { a: { indexed: 5, embedded: 2, embeddable: 4 }, b: { indexed: 4, embedded: 4, embeddable: 4 } } };
  assert.deepEqual(paperIndexed(index, 'a'), ['search.paperEmbedding', { done: 2, total: 4 }]);
  assert.deepEqual(paperIndexed(index, 'b'), ['search.paperIndexed', {}]);
  assert.deepEqual(paperIndexed(index, 'c'), ['search.paperNotIndexed', {}]);
  assert.deepEqual(paperIndexed({ ...index, mode: 'keyword_only' }, 'a'), ['search.paperKeywordOnly', {}]);
  assert.deepEqual(paperIndexed(null, 'a'), ['search.paperNotIndexed', {}]);
});

test("an offer's outcome is said in the interface's words, a refusal by its error's", () => {
  assert.equal(offerOutcome(t, { answer: 'modelscope', outcome: 'started' }), t('search.offer.started'));
  assert.equal(offerOutcome(t, { answer: 'later' }), t('search.offer.later'));
  assert.equal(offerOutcome(t, { answer: 'huggingface', outcome: 'disk_full' }), t('errors.disk_full'));
  assert.equal(offerOutcome(t, { answer: 'huggingface', outcome: 'never_seen' }), t('errors.internal'));
  assert.equal(offerOutcome(t, null), null);
  for (const said of ['started', 'download_running', 'already_installed', 'not_needed', 'local_only', 'later']) {
    for (const catalog of [en, zhCN]) assert.ok(`search.offer.${said}` in catalog, said);
  }
});

test('the offer asks in its own words, with the three options in order', () => {
  for (const catalog of [en, zhCN]) {
    for (const key of ['ask.model_download.question', 'ask.model_download.body', 'ask.kindName.model_download',
      'ask.model_download.option.huggingface', 'ask.model_download.option.modelscope', 'ask.model_download.option.later',
      'ask.answer.huggingface', 'ask.answer.modelscope', 'ask.answer.later', 'settings.workflowIndex', 'settings.workflowModelOffer']) {
      assert.ok(key in catalog, key);
    }
  }
  assert.equal(en['ask.model_download.option.huggingface'], 'Download from Hugging Face');
  assert.equal(en['ask.model_download.option.modelscope'], 'Download from ModelScope');
  assert.equal(en['ask.model_download.option.later'], 'Later');
});

test('an index run or a paper says why search is keyword-only, in words for each reason', () => {
  assert.equal(indexReason('deadline'), 'search.reason.deadline');
  assert.equal(indexReason('model_missing'), 'helper.reason.model_missing');
  assert.equal(indexReason('model_missing', false), 'helper.reasonImport.model_missing'); // Local only: import
  assert.equal(indexReason('never_seen'), 'helper.reason.other');
  for (const reason of ['request_failed', 'start_failed', 'start_timeout', 'helper_failed', 'index_unavailable', 'model_changed',
    'binary_missing', 'binary_changed', 'crashed', 'unhealthy', 'helper_unavailable']) {
    for (const catalog of [en, zhCN]) assert.ok(`errors.${reason}` in catalog, reason); // a failed index run's reason
  }
});
