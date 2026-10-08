// The local model helper's status as the interface reads it (src/helper.js), and the catalog
// entries the section and the consent screen name.
import test from 'node:test';
import assert from 'node:assert/strict';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };
import { downloadOffered, downloadOutcome, downloading, helperState, keywordOnlyReason, modelFilePicker, pollDelay,
  preferredSource, progress, SOURCES } from '../src/helper.js';

test('the consent screen selects ModelScope once Hugging Face could not be reached, else the last choice', () => {
  assert.equal(preferredSource({ model_source: null, recommended_source: null }), 'huggingface');
  assert.equal(preferredSource({ model_source: 'modelscope', recommended_source: null }), 'modelscope');
  assert.equal(preferredSource({ model_source: 'huggingface', recommended_source: 'modelscope' }), 'modelscope');
  assert.equal(preferredSource(null), 'huggingface');
});

test('a download shows how far it has come', () => {
  assert.equal(progress({ received: 0, total: 0 }), 0);
  assert.equal(progress({ received: 50, total: 200 }), 0.25);
  assert.equal(progress({ received: 300, total: 200 }), 1);
  assert.equal(progress(null), 0);
  assert.equal(downloading({ download: { state: 'running' } }), true);
  assert.equal(downloading({ download: { state: 'cancelled' } }), false);
});

test('search says why it is keyword-only, with an entry for every reason', () => {
  assert.equal(keywordOnlyReason({ search: { mode: 'hybrid', reason: null } }), null);
  assert.equal(keywordOnlyReason({ search: { mode: 'keyword_only', reason: 'model_missing' } }), 'helper.reason.model_missing');
  assert.equal(keywordOnlyReason({ search: { mode: 'keyword_only', reason: 'something new' } }), 'helper.reason.other');
  for (const reason of ['model_missing', 'model_changed', 'binary_missing', 'binary_changed', 'start_timeout',
    'start_failed', 'crashed', 'unhealthy', 'helper_failed', 'other']) {
    for (const catalog of [en, zhCN]) assert.ok(Object.hasOwn(catalog, `helper.reason.${reason}`), reason);
  }
});

test('Advanced offers the download unless the current project is Local only', () => {
  assert.equal(downloadOffered({ kind: 'research', sensitivity: 'local_only', review_lock: false }), false);
  assert.equal(downloadOffered({ kind: 'research', sensitivity: 'local_only', review_lock: true }), false);
  for (const project of [{ kind: 'research', sensitivity: 'normal' }, { kind: 'research', sensitivity: 'private' },
    { kind: 'general', sensitivity: 'normal' }, null, undefined]) {
    assert.equal(downloadOffered(project), true, JSON.stringify(project));
  }
});

test('where no download is offered, a missing or changed model is to be imported, not downloaded', () => {
  const keywordOnly = (reason) => ({ search: { mode: 'keyword_only', reason } });
  assert.equal(keywordOnlyReason(keywordOnly('model_missing'), false), 'helper.reasonImport.model_missing');
  assert.equal(keywordOnlyReason(keywordOnly('model_changed'), false), 'helper.reasonImport.model_changed');
  assert.equal(keywordOnlyReason(keywordOnly('crashed'), false), 'helper.reason.crashed');
  assert.equal(keywordOnlyReason(keywordOnly('something new'), false), 'helper.reason.other');
  assert.equal(keywordOnlyReason(keywordOnly('model_missing')), 'helper.reason.model_missing');
  assert.equal(keywordOnlyReason({ search: { mode: 'hybrid', reason: null } }, false), null);
  for (const reason of ['model_missing', 'model_changed']) {
    const key = `helper.reasonImport.${reason}`;
    assert.match(en[key], /Import/, key);
    assert.doesNotMatch(en[key], /download/i, key);
    assert.match(zhCN[key], /导入/, key);
    assert.doesNotMatch(zhCN[key], /下载/, key);
  }
});

// Every code a download can end with (backend/local_helper.py Local._download).
const DOWNLOAD_FAILURES = ['source_unreachable', 'source_refused', 'size_mismatch', 'hash_mismatch', 'redirect_refused',
  'download_refused', 'download_interrupted', 'disk_full', 'write_failed', 'closing', 'database_unavailable',
  'notice_missing', 'download_failed'];

test('a download that ended without installing the model gives its own advice only where downloads are offered', () => {
  for (const code of DOWNLOAD_FAILURES) {
    const failed = { state: 'failed', problem: code };
    assert.equal(downloadOutcome(failed), `errors.${code}`, code);
    assert.equal(downloadOutcome(failed, true), `errors.${code}`, code);
    assert.equal(downloadOutcome(failed, false), 'helper.downloadEndedImport', code);
  }
  assert.equal(downloadOutcome({ state: 'cancelled', problem: null }, true), 'helper.downloadCancelled');
  assert.equal(downloadOutcome({ state: 'cancelled', problem: null }, false), 'helper.downloadEndedImport');
  for (const download of [null, undefined, { state: 'running' }, { state: 'done' }]) {
    assert.equal(downloadOutcome(download, true), null);
    assert.equal(downloadOutcome(download, false), null);
  }
  // The one line for every outcome there: that the model is not installed, and the import; never a download.
  assert.match(en['helper.downloadEndedImport'], /did not install.*Import the model file/);
  assert.doesNotMatch(en['helper.downloadEndedImport'], /try|again|other source|download it/i);
  assert.match(zhCN['helper.downloadEndedImport'], /没有安装.*导入模型文件/);
  assert.doesNotMatch(zhCN['helper.downloadEndedImport'], /再试|重试|另一个来源|重新下载/);
});

test('the Local only note points to the import under Settings, Advanced, never to a download or an install', () => {
  assert.match(en['helper.localOnlyBody'], /keyword-only until the search model is installed or imported/);
  assert.match(en['helper.localOnlyBody'], /import the model file under Settings, Advanced\.$/);
  assert.doesNotMatch(en['helper.localOnlyBody'], /install it|all projects/);
  assert.match(zhCN['helper.localOnlyBody'], /可在“设置 › 高级”中导入模型文件。$/);
  assert.doesNotMatch(zhCN['helper.localOnlyBody'], /所有项目|安装它/);
  for (const catalog of [en, zhCN]) assert.ok(Object.hasOwn(catalog, 'errors.local_only_no_download'));
});

test('no string calls the interface bilingual', () => {
  for (const catalog of [en, zhCN]) {
    for (const [key, value] of Object.entries(catalog)) assert.doesNotMatch(JSON.stringify(value), /bilingual|双语/i, key);
  }
});

test('a stopped helper that no request can start is shown as unavailable, not as waiting for search', () => {
  const keywordOnly = { mode: 'keyword_only', reason: 'model_changed' };
  assert.equal(helperState({ helper: { state: 'stopped' }, search: { mode: 'hybrid', reason: null } }), 'stopped');
  assert.equal(helperState({ helper: { state: 'stopped' }, search: keywordOnly }), 'unavailable');
  assert.equal(helperState({ helper: { state: 'restarting' }, search: { mode: 'keyword_only', reason: 'crashed' } }),
    'restarting');
  assert.equal(helperState({ helper: { state: 'failed' }, search: { mode: 'keyword_only', reason: 'helper_failed' } }),
    'failed');
  assert.equal(helperState({ helper: null, search: keywordOnly }), 'unavailable');
});

test('every state, source and failure the backend reports has its words in both catalogs', () => {
  const keys = [
    ...['stopped', 'starting', 'running', 'restarting', 'failed', 'unavailable'].map((state) => `helper.state.${state}`),
    ...SOURCES.map((source) => `helper.source.${source}`),
    ...['source_unreachable', 'source_refused', 'size_mismatch', 'hash_mismatch', 'redirect_refused', 'download_refused',
      'download_interrupted', 'disk_full', 'write_failed', 'closing', 'invalid_path', 'file_not_found', 'not_a_file',
      'file_unreadable', 'import_failed', 'local_only_no_download', 'already_installed', 'download_running',
      'unknown_model', 'unknown_source', 'no_download', 'download_failed', 'database_unavailable', 'notice_missing',
    ].map((code) => `errors.${code}`),
  ];
  for (const key of keys) {
    assert.ok(Object.hasOwn(en, key), key);
    assert.ok(Object.hasOwn(zhCN, key), key);
  }
});

test('a refused file is said not to be installed, never not to be kept: a partial copy may remain', () => {
  // A .part file that cannot be removed is left for the next launch (backend/local_helper.py _remove).
  for (const key of ['errors.size_mismatch', 'errors.hash_mismatch', 'helper.consentNote', 'helper.importHint']) {
    assert.match(en[key], /install/, key);
    assert.doesNotMatch(en[key], /\bkeeps?\b|\bkept\b|\bcopies\b/, key);
    assert.match(zhCN[key], /安装/, key);
    assert.doesNotMatch(zhCN[key], /保留|复制/, key);
  }
});

test('the status is read often only while something is under way', () => {
  assert.equal(pollDelay({ download: { state: 'running' }, helper: { state: 'stopped' } }), 1000);
  assert.equal(pollDelay({ download: null, helper: { state: 'starting' } }), 1000);
  assert.equal(pollDelay({ download: { state: 'done' }, helper: { state: 'running' } }), 4000);
  assert.equal(pollDelay(null), 4000);
});

test('the model file picker is the window bridge, where there is one', () => {
  assert.equal(modelFilePicker({}), null);
  assert.equal(modelFilePicker({ pywebview: { api: { choose_folder: () => 'x' } } }), null);
  const pick = modelFilePicker({ pywebview: { api: { choose_model_file: () => '/m.gguf' } } });
  assert.equal(pick(), '/m.gguf');
});
