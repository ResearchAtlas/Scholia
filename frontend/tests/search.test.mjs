// Search as the interface reads it (src/search.js; S1-17): the field's keys, input methods included,
// blank input, what results say of how they were found, where a passage is, a paper's index line,
// the offer's outcome, and the catalog entries each names.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };
import { loadLanguage, makeT } from '../src/i18n/index.js';
import { fieldKey, indexCounts, indexReason, offerOutcome, onFieldKey, paperIndexed, queryOf, rebuildAfter, runCounts, runReason, searchNote, whereIs } from '../src/search.js';

await loadLanguage('zh-CN'); // a part of its own (i18n/index.js), loaded before Chinese is drawn

const t = makeT('en');

test('Enter searches, Escape clears, and Enter while an input method composes does neither', () => {
  assert.equal(fieldKey({ key: 'Enter', isComposing: false, keyCode: 13 }), 'search');
  assert.equal(fieldKey({ key: 'Enter', isComposing: true, keyCode: 13 }), 'compose'); // choosing 最低工资 in pinyin
  assert.equal(fieldKey({ key: 'Enter', isComposing: false, keyCode: 229 }), 'compose'); // WebKit's composing Enter
  assert.equal(fieldKey({ key: 'Escape', isComposing: false, keyCode: 27 }), 'clear');
  assert.equal(fieldKey({ key: 'a', isComposing: false, keyCode: 65 }), null);
});

test('an Enter confirming composed characters keeps its default, so the input method commits them, and holds off the search', () => {
  const pressed = (key, isComposing, keyCode) => {
    const event = { key, keyCode, nativeEvent: { isComposing }, prevented: 0, preventDefault() { this.prevented += 1; } };
    const cleared = [];
    const hold = onFieldKey(event, () => cleared.push(true));
    return { hold, prevented: event.prevented, cleared: cleared.length };
  };
  assert.deepEqual(pressed('Enter', true, 13), { hold: true, prevented: 0, cleared: 0 }); // connector thread 4237098120
  assert.deepEqual(pressed('Enter', false, 229), { hold: true, prevented: 0, cleared: 0 });
  assert.deepEqual(pressed('Enter', false, 13), { hold: false, prevented: 0, cleared: 0 }); // the form's submission searches
  assert.deepEqual(pressed('Escape', false, 27), { hold: false, prevented: 1, cleared: 1 });
  assert.deepEqual(pressed('a', false, 65), { hold: false, prevented: 0, cleared: 0 });
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
  for (const reason of ['deadline', 'vectors_unavailable', 'index_unavailable', 'request_failed', 'input_refused', 'closing']) {
    for (const catalog of [en, zhCN]) assert.ok(`search.reason.${reason}` in catalog, reason);
  }
  assert.equal(note({ mode: 'keyword_only', reason: 'input_refused' }), // a question the search model refused
    'Search is keyword-only: the search model could not take this question. Try other words.');
  assert.equal(searchNote(null), null);
});

test('a result says its page and section path', () => {
  assert.equal(whereIs(t, { page: 3, section_path: ['Methods', 'Data'] }), 'p. 3 · Methods › Data');
  assert.equal(whereIs(t, { page: null, section_path: ['研究发现'] }), '研究发现');
  assert.equal(whereIs(t, { page: null, section_path: [] }), '');
});

test('passages the search model could not take are counted, not taken for embeddings still to come', () => {
  const note = (coverage) => { const said = searchNote({ mode: 'hybrid', index: 'ready', coverage }); return said && t(...said); };
  assert.equal(note({ embedded: 3, total: 4, refused: 1 }),
    'Meaning search covers 3 of 4 passages; 1 could not be taken by the search model and is found by keywords.');
  assert.equal(note({ embedded: 2, total: 4, refused: 2 }),
    'Meaning search covers 2 of 4 passages; 2 could not be taken by the search model and are found by keywords.');
  assert.equal(note({ embedded: 2, total: 4, refused: 1 }), // one still to come
    'Meaning search covers 2 of 4 passages so far; the others are found by keywords.');
  const index = { mode: 'hybrid', materials: { a: { indexed: 5, embedded: 3, embeddable: 4, refused: 1 },
    b: { indexed: 5, embedded: 2, embeddable: 4, refused: 1 } } };
  assert.equal(t(...paperIndexed(index, 'a')),
    'Indexed for keyword and meaning search, except 1 passage the search model could not take; keywords still find it.');
  assert.equal(t(...paperIndexed(index, 'b')), // still embedding: the refusal is said beside the progress
    'Indexed for keyword search; embeddings 2 of 4; 1 could not be embedded');
  assert.equal(makeT('zh-CN')(...paperIndexed(index, 'b')), '已建立关键词索引；向量 2/4；1 个无法生成向量');
  const none = { mode: 'hybrid', materials: { c: { indexed: 3, embedded: 0, embeddable: 2, refused: 2 } } };
  assert.equal(t(...paperIndexed(none, 'c')), 'Indexed for keyword search only: the search model could not take its 2 passages.');
  assert.equal(t(...indexCounts({ indexed: 12, embedded: 9, embeddable: 11, refused: 2 })),
    '12 indexed; embeddings 9 of 11; 2 could not be embedded');
  assert.equal(t(...indexCounts({ indexed: 12, embedded: 11, embeddable: 11, refused: 0 })), '12 indexed; embeddings 11 of 11');
  assert.equal(t(...runCounts({ status: 'succeeded', result: { mode: 'hybrid', embedded: 6,
    passages: { indexed: 12, embedded: 10, embeddable: 11, refused: 1 } } })),
  '6 embedded by this run; embeddings 10 of 11 in this project; 1 could not be embedded');
  const zh = makeT('zh-CN');
  assert.equal(zh(...paperIndexed(index, 'a')), '已建立关键词与语义索引，但有 1 个段落搜索模型无法处理；仍可按关键词找到。');
  assert.equal(zh(...indexCounts({ indexed: 12, embedded: 9, embeddable: 11, refused: 2 })), '已索引 12 个；向量 9/11；2 个无法生成向量');
  for (const key of ['search.partialRefused', 'search.paperRefused', 'search.paperRefusedAll', 'search.paperEmbeddingRefused',
    'search.indexCountsRefused',
    'search.runHybridRefused']) {
    for (const catalog of [en, zhCN]) assert.ok(key in catalog, key);
  }
  const source = readFileSync(fileURLToPath(new URL('../src/components/SearchIndex.jsx', import.meta.url)), 'utf8');
  assert.match(source, /t\(\.\.\.indexCounts\(index\.passages\)\)/);
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
  for (const reason of ['request_failed', 'start_failed', 'start_timeout', 'helper_failed', 'index_unavailable', 'superseded', 'model_changed',
    'binary_missing', 'binary_changed', 'crashed', 'unhealthy', 'helper_unavailable']) {
    for (const catalog of [en, zhCN]) assert.ok(`errors.${reason}` in catalog, reason); // a failed index run's reason
  }
});

test("every view of the index, a search or an index run words its reason by indexReason, as its project offers the model", () => {
  const source = (name) => readFileSync(fileURLToPath(new URL(`../src/components/${name}`, import.meta.url)), 'utf8');
  for (const name of ['SearchIndex.jsx', 'Library.jsx', 'Settings.jsx']) {
    assert.doesNotMatch(source(name), /keywordOnlyReason/, name); // the helper's own reasons only (LocalHelper.jsx)
  }
  for (const name of ['SearchIndex.jsx', 'Library.jsx']) { // the project open: its own offer
    const calls = [...source(name).matchAll(/indexReason\(([^()]*(?:\([^()]*\))?[^()]*)\)/g)].map((m) => m[1]);
    assert.ok(calls.length > 0, name);
    for (const args of calls) assert.match(args, /downloadOffered\(project\)/, `${name}: indexReason(${args})`);
  }
  // The background-run list holds every project's runs: each row is worded by its own project.
  assert.match(source('Settings.jsx'), /t\(runReason\(run\)\)/);
  assert.doesNotMatch(source('Settings.jsx'), /indexReason\(/);
  // A healthy helper with sqlite-vec missing, or the index unavailable, is not "the helper is not available".
  for (const reason of ['vectors_unavailable', 'index_unavailable']) {
    assert.notEqual(t(indexReason(reason)), t('helper.reason.other'), reason);
    assert.notEqual(t(indexReason(reason)), t('helper.reason.helper_failed'), reason);
  }
});

test("an index run's reason is worded as its own project offers the model, whichever project is open", () => {
  const run = (offered) => ({ workflow: 'index', result: { mode: 'keyword_only', reason: 'model_missing' }, download_offered: offered });
  assert.equal(runReason(run(true)), 'helper.reason.model_missing'); // Download it, or import its file
  assert.equal(runReason(run(false)), 'helper.reasonImport.model_missing'); // a Local only project: import only
  assert.match(t(runReason(run(true))), /Download/);
  assert.doesNotMatch(t(runReason(run(false))), /Download/);
  assert.equal(runReason({ result: { reason: 'vectors_unavailable' }, download_offered: false }), 'search.reason.vectors_unavailable');
});

test("an index run is its project's: the list says what it did in counts, and whether it rebuilt", () => {
  const passages = { indexed: 12, embedded: 10, embeddable: 11 };
  assert.deepEqual(runCounts({ status: 'succeeded', result: { mode: 'hybrid', embedded: 6, passages } }),
    ['search.runHybrid', { embedded: 6, done: 10, total: 11 }]);
  assert.equal(t(...runCounts({ status: 'succeeded', result: { mode: 'hybrid', embedded: 6, passages } })),
    '6 embedded by this run; embeddings 10 of 11 in this project');
  assert.deepEqual(runCounts({ status: 'succeeded', result: { mode: 'keyword_only', reason: 'model_missing', passages } }),
    ['search.runKeyword', { indexed: 12 }]);
  assert.equal(runCounts({ status: 'failed', result: { reason: 'request_failed' } }), null);
  assert.equal(runCounts({ status: 'running', result: null }), null);
  for (const key of ['search.runHybrid', 'search.runKeyword', 'settings.workflowIndexRebuild']) {
    for (const catalog of [en, zhCN]) assert.ok(key in catalog, key);
  }
  const settings = readFileSync(fileURLToPath(new URL('../src/components/Settings.jsx', import.meta.url)), 'utf8');
  assert.match(settings, /run\.workflow === 'index' && run\.rebuild \? t\('settings\.workflowIndexRebuild'\)/);
  assert.match(settings, /t\(\.\.\.runCounts\(run\)\)/);
});

test('a Rebuild asked while another index run ran follows that run, then the rebuild its end records', () => {
  const during = { run_id: 'r1', rebuild_pending: true };
  const next = { run: { run_id: 'r2', status: 'running', rebuild: true }, last_rebuild: { run_id: 'r2', status: 'running', after: 'r1' } };
  assert.equal(rebuildAfter(during, { status: 'succeeded' }, next), 'r2');
  assert.equal(rebuildAfter(during, { status: 'failed' }, next), 'r2'); // a run that fails still runs its rerun
  assert.equal(rebuildAfter(during, { status: 'cancelled' }, next), null); // Cancel drops the rerun (R29)
  // PR 29 finding 53: the rebuild ended before the interface looked, and an ordinary rerun runs now: still that rebuild
  assert.equal(rebuildAfter(during, { status: 'succeeded' }, { run: { run_id: 'r3', status: 'running', rebuild: false },
    last_rebuild: { run_id: 'r2', status: 'succeeded', after: 'r1' } }), 'r2');
  // an older rebuild, recorded after another run (that run's end recorded none: interrupted), is not it
  assert.equal(rebuildAfter(during, { status: 'interrupted' }, { run: { run_id: 'r1', rebuild: false },
    last_rebuild: { run_id: 'r0', status: 'succeeded', after: 'rx' } }), null);
  assert.equal(rebuildAfter({ run_id: 'r2', rebuild_pending: false }, { status: 'succeeded' }, next), null);
  for (const catalog of [en, zhCN]) assert.ok('search.rebuildAfterRun' in catalog);
  const component = readFileSync(fileURLToPath(new URL('../src/components/SearchIndex.jsx', import.meta.url)), 'utf8');
  assert.match(component, /rebuildAfter\(started, ended, await indexStatus\(project\.id, started\.run_id\)/);
  assert.match(component, /index\.run\?\.rebuild_pending && \(/);
});
