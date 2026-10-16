// The interface languages' catalogs (i18n/index.js): English is loaded with the window, Chinese is a
// part of its own loaded only when it is asked for; the window is drawn in a language only once its
// catalog is here, so nothing shows in another language meanwhile, and a catalog that cannot load
// shows as a part that could not load (App.jsx), never as English.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { LANGUAGES, languageLoaded, languageSwitch, loadLanguage, makeT } from '../src/i18n/index.js';
import { NotLoaded } from '../src/parts.js';

// A load that settles when the test says.
function pending() {
  const loads = [];
  const load = (language) => new Promise((resolve, reject) => loads.push({ language, resolve, reject }));
  return { load, loads };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('the Chinese catalog loads when Chinese is asked for, and not otherwise', async () => {
  assert.deepEqual(LANGUAGES, ['en', 'zh-CN']); // listed without loading it
  assert.equal(languageLoaded('en'), true);
  assert.equal(languageLoaded('zh-CN'), false);
  const shown = [];
  const want = languageSwitch((state) => shown.push(state));
  want('en');
  assert.deepEqual(shown, [{ language: 'en', failed: false }]); // English is drawn at once
  await settle();
  assert.equal(languageLoaded('zh-CN'), false);
  want('zh-CN');
  assert.equal(shown.length, 1); // still English until Chinese has loaded: the change waits for it
  await loadLanguage('zh-CN'); // the load want started, loaded once
  await settle();
  assert.deepEqual(shown.at(-1), { language: 'zh-CN', failed: false });
  assert.equal(languageLoaded('zh-CN'), true);
  assert.equal(makeT('zh-CN')('sidebar.projects'), '项目');
});

test('at launch nothing is drawn until the language\'s catalog has loaded, then it is drawn in that language', async () => {
  const { load, loads } = pending();
  const shown = [];
  languageSwitch((state) => shown.push(state), load)('xx');
  await settle();
  assert.deepEqual(shown, []); // no English meanwhile: the window draws nothing
  loads[0].resolve();
  await settle();
  assert.deepEqual(shown, [{ language: 'xx', failed: false }]);
});

test('the last language asked for wins over one still loading', async () => {
  const { load, loads } = pending();
  const shown = [];
  const want = languageSwitch((state) => shown.push(state), load);
  want('xx');
  want('en'); // the setting read at launch says English
  loads[0].resolve();
  await settle();
  assert.deepEqual(shown, [{ language: 'en', failed: false }]);

  want('xx');
  want('yy');
  loads[2].resolve();
  loads[1].resolve();
  await settle();
  assert.deepEqual(shown.at(-1), { language: 'yy', failed: false });
  assert.equal(shown.length, 2);
});

test('a catalog that cannot load shows as failed, never as English', async () => {
  const { load, loads } = pending();
  const shown = [];
  const want = languageSwitch((state) => shown.push(state), load);
  want('xx');
  loads[0].reject(new NotLoaded('Failed to fetch dynamically imported module'));
  await settle();
  assert.deepEqual(shown, [{ language: null, failed: true }]); // at launch: nothing was drawn yet

  const changed = [];
  const change = languageSwitch((state) => changed.push(state), load);
  change('en');
  change('xx'); // changed in Settings
  loads[1].reject(new NotLoaded('refused'));
  await settle();
  assert.deepEqual(changed, [{ language: 'en', failed: false }, { language: 'en', failed: true }]);

  const stale = [];
  const later = languageSwitch((state) => stale.push(state), load);
  later('xx');
  later('en'); // no longer asked for: its failure changes nothing
  loads[2].reject(new NotLoaded('refused'));
  await settle();
  assert.deepEqual(stale, [{ language: 'en', failed: false }]);
});

test('the window shows a failed catalog as a part that could not load, with Try again, and nothing before one loads', () => {
  const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8');
  assert.match(app, /if \(shown\.failed\) \{[\s\S]*?<LoadState problem="part_not_loaded" onRetry=\{\(\) => window\.location\.reload\(\)\} \/>/);
  assert.match(app, /if \(!language\) return null;/);
});
