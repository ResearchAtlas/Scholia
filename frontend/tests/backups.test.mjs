// Backups, restore, export and deletion as the interface asks for them (src/backups.js), and
// the catalog entries the components name.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import en from '../src/i18n/en.json' with { type: 'json' };
import { makeT } from '../src/i18n/index.js';
import { deletePath, deletionNotices, fileSize, folderPicker, needsPassphrase, restoreBody } from '../src/backups.js';
import { errorText } from '../src/text.js';

test('a deletion asks for the backups and the trace only when chosen', () => {
  assert.equal(deletePath('conversation', 'c1'), '/api/conversations/c1');
  assert.equal(deletePath('project', 'p1', { everywhere: true }), '/api/projects/p1?purge_backups=true');
  assert.equal(deletePath('project', 'p1', { removeAllTrace: true }), '/api/projects/p1?remove_all_trace=true');
  assert.equal(deletePath('conversation', 'a/b', { everywhere: true, removeAllTrace: true }),
    '/api/conversations/a%2Fb?purge_backups=true&remove_all_trace=true');
});

test('a deletion that succeeded says what it could not finish', () => {
  assert.deepEqual(deletionNotices({ ok: true }), []);
  assert.deepEqual(deletionNotices({ ok: true, files_left: true }), ['delete.filesLeft']);
  assert.deepEqual(deletionNotices({ ok: true, purge_failed: true, files_left: true }),
    ['delete.purgeFailed', 'delete.filesLeft']);
  for (const key of deletionNotices({ purge_failed: true, files_left: true })) {
    assert.ok(Object.hasOwn(en, key), key);
  }
});

test('a restore names one automatic backup, or a file with its passphrase when there is one', () => {
  assert.deepEqual(restoreBody({ generation: 'daily/20261003T030000000000Z', file: 'ignored' }),
    { generation: 'daily/20261003T030000000000Z' });
  assert.deepEqual(restoreBody({ file: ' /Users/r/b.zip ', passphrase: '' }), { file: '/Users/r/b.zip' });
  assert.deepEqual(restoreBody({ file: '/Users/r/b.zip', passphrase: 'p' }), { file: '/Users/r/b.zip', passphrase: 'p' });
});

test('a Private or Local only project needs a passphrase', () => {
  assert.equal(needsPassphrase([{ sensitivity: 'normal' }, { kind: 'general', sensitivity: 'normal' }]), false);
  assert.equal(needsPassphrase([{ sensitivity: 'normal' }, { sensitivity: 'private' }]), true);
  assert.equal(needsPassphrase([{ sensitivity: 'local_only' }]), true);
  assert.equal(needsPassphrase([]), false);
});

test('sizes read in the interface language', () => {
  assert.equal(fileSize(512, 'en'), '0.5 kB');
  assert.equal(fileSize(1_234_567, 'en'), '1.2 MB');
  assert.equal(fileSize(5_000_000_000, 'en'), '5 GB');
  assert.match(fileSize(1_234_567, 'zh-CN'), /^1\.2\s?MB$/);
  assert.equal(fileSize(undefined, 'en'), '0 kB');
});

test('the folder picker is the window\'s, and there is none in a browser', async () => {
  assert.equal(folderPicker({}), null);
  assert.equal(folderPicker({ pywebview: { api: {} } }), null);
  const pick = folderPicker({ pywebview: { api: { choose_folder: async () => '/Users/r/Backups' } } });
  assert.equal(await pick(), '/Users/r/Backups');
});

test('every new error code reads in each language, never as the backend\'s message', () => {
  const codes = ['passphrase_required', 'wrong_passphrase', 'backup_busy', 'database_damaged', 'database_unavailable',
    'closing', 'backup_failed', 'write_failed', 'disk_full', 'destination_not_writable', 'destination_not_found',
    'destination_in_data_folder', 'invalid_destination', 'not_a_backup', 'backup_damaged', 'backup_unreadable',
    'newer_schema', 'restore_failed', 'safety_copy_failed', 'data_folder_problem', 'data_folder_synced',
    'data_folder_not_empty', 'data_folder_not_found', 'data_folder_unsafe', 'data_folder_invalid'];
  for (const language of ['en', 'zh-CN']) {
    const t = makeT(language);
    for (const code of codes) assert.notEqual(errorText(t, code), t('errors.internal'), `${language}: ${code}`);
  }
});

test('every catalog key a component names is in the catalog', () => {
  const src = fileURLToPath(new URL('../src/', import.meta.url));
  const missing = readdirSync(src, { recursive: true })
    .filter((name) => /\.(jsx|js)$/.test(name))
    .flatMap((name) => [...readFileSync(join(src, name), 'utf8').matchAll(/\bt\('([\w.]+)'/g)]
      .map((match) => match[1]).filter((key) => !Object.hasOwn(en, key)).map((key) => `${name}: ${key}`));
  assert.deepEqual(missing, []);
});
