// The interface languages' catalogs (i18n/index.js): English is loaded with the window, Chinese is a
// part of its own loaded only when it is asked for; the window is drawn in a language only once its
// catalog is here, so nothing shows in another language meanwhile. Only the setting's language is
// loaded at launch. A catalog that cannot load at launch is said so in its own language (NOT_LOADED),
// with Try again and Use English, never as English; a change in Settings loads first and saves after.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { LANGUAGES, NOT_LOADED, languageLoaded, languageSwitch, loadLanguage, makeT } from '../src/i18n/index.js';
import { NotLoaded } from '../src/parts.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };

// A load that settles when the test says.
function pending() {
  const loads = [];
  const load = (language) => new Promise((resolve, reject) => loads.push({ language, resolve, reject }));
  return { load, loads };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8');
const HAN = /\p{Script=Han}/u;

test('the Chinese catalog loads when Chinese is asked for, and not otherwise', async () => {
  assert.deepEqual(LANGUAGES, ['en', 'zh-CN']); // listed without loading it
  assert.equal(languageLoaded('en'), true);
  assert.equal(languageLoaded('zh-CN'), false);
  const shown = [];
  const { want } = languageSwitch((state) => shown.push(state));
  want('en');
  assert.deepEqual(shown, [{ language: 'en', failed: null }]); // English is drawn at once
  await settle();
  assert.equal(languageLoaded('zh-CN'), false);
  want('zh-CN');
  assert.equal(shown.length, 1); // still English until Chinese has loaded: the change waits for it
  await loadLanguage('zh-CN'); // the load want started, loaded once
  await settle();
  assert.deepEqual(shown.at(-1), { language: 'zh-CN', failed: null });
  assert.equal(languageLoaded('zh-CN'), true);
  assert.equal(makeT('zh-CN')('sidebar.projects'), '项目');
});

test('at launch nothing is drawn until the language\'s catalog has loaded, then it is drawn in that language', async () => {
  const { load, loads } = pending();
  const shown = [];
  languageSwitch((state) => shown.push(state), load).want('xx');
  await settle();
  assert.deepEqual(shown, []); // no English meanwhile: the window draws nothing
  loads[0].resolve();
  await settle();
  assert.deepEqual(shown, [{ language: 'xx', failed: null }]);
});

test('the system language is a fallback only: asked for when no language has been, else nothing loads', async () => {
  const { load, loads } = pending();
  const shown = [];
  const { want, fallback } = languageSwitch((state) => shown.push(state), load);
  await want('en'); // the setting, read
  await fallback('xx'); // a later read failed: the system's language is not loaded
  assert.deepEqual(loads, []);
  assert.deepEqual(shown, [{ language: 'en', failed: null }]);

  const unread = languageSwitch((state) => shown.push(state), load);
  const drawn = unread.fallback('xx'); // the setting could not be read: the system's language
  assert.deepEqual(loads.map((entry) => entry.language), ['xx']);
  loads[0].resolve();
  await drawn;
  assert.deepEqual(shown.at(-1), { language: 'xx', failed: null });
});

test('the last language asked for wins over one still loading', async () => {
  const { load, loads } = pending();
  const shown = [];
  const { want } = languageSwitch((state) => shown.push(state), load);
  want('xx');
  want('en'); // the setting read at launch says English
  loads[0].resolve();
  await settle();
  assert.deepEqual(shown, [{ language: 'en', failed: null }]);

  want('xx');
  want('yy');
  loads[2].resolve();
  loads[1].resolve();
  await settle();
  assert.deepEqual(shown.at(-1), { language: 'yy', failed: null });
  assert.equal(shown.length, 2);
});

test('a catalog that cannot load is shown as failed, naming the language, never as English', async () => {
  const { load, loads } = pending();
  const shown = [];
  languageSwitch((state) => shown.push(state), load).want('xx');
  loads[0].reject(new NotLoaded('Failed to fetch dynamically imported module'));
  await settle();
  assert.deepEqual(shown, [{ language: null, failed: 'xx' }]); // at launch: nothing was drawn yet

  const stale = [];
  const later = languageSwitch((state) => stale.push(state), load);
  later.want('xx');
  later.want('en'); // no longer asked for: its failure changes nothing
  loads[1].reject(new NotLoaded('refused'));
  await settle();
  assert.deepEqual(stale, [{ language: 'en', failed: null }]);
});

test('Use English after a failed load draws English: the failure is gone', async () => {
  const { load, loads } = pending();
  const shown = [];
  const { want } = languageSwitch((state) => shown.push(state), load);
  const failing = want('xx');
  loads[0].reject(new NotLoaded('refused'));
  assert.equal(await failing, undefined); // never rejects: the failure is in what show was told
  await want('en');
  assert.deepEqual(shown, [{ language: null, failed: 'xx' }, { language: 'en', failed: null }]);
});

test('want settles once the language is drawn, so what is drawn next waits for it', async () => {
  const { load, loads } = pending();
  const shown = [];
  const { want } = languageSwitch((state) => shown.push(state), load);
  let settled = false;
  const drawn = want('xx').then(() => { settled = true; });
  await settle();
  assert.equal(settled, false); // the setting's language is still loading: the window waits
  loads[0].resolve();
  await drawn;
  assert.deepEqual(shown, [{ language: 'xx', failed: null }]);
});

test('the screen of a language that cannot load speaks that language, from strings loaded with the window', () => {
  for (const language of LANGUAGES) {
    const text = NOT_LOADED[language];
    assert.deepEqual(Object.keys(text), ['message', 'retry', 'english'], language);
    assert.ok(Object.values(text).every((value) => typeof value === 'string' && value), language);
  }
  assert.equal(NOT_LOADED.en.retry, en['common.retry']);
  assert.equal(NOT_LOADED['zh-CN'].retry, zhCN['common.retry']);
  for (const value of Object.values(NOT_LOADED['zh-CN'])) assert.match(value, HAN);
  for (const value of Object.values(NOT_LOADED.en)) assert.doesNotMatch(value, HAN);
  const app = source('App.jsx');
  assert.match(app, /if \(shown\.failed\) return <LanguageNotLoaded language=\{shown\.failed\} onEnglish=\{chooseEnglish\} \/>;/);
  assert.match(app, /const text = NOT_LOADED\[language\];/);
  assert.match(app, /onClick=\{\(\) => window\.location\.reload\(\)\}>\{text\.retry\}/);
  assert.match(app, /onClick=\{onEnglish\}>\{text\.english\}/);
  assert.match(app, /if \(!language\) return null;/);
  assert.doesNotMatch(app, /LanguageContext\.Provider value=\{language \?\? 'en'\}/);
});

test('Use English saves the setting as English (once more if that did not take), then draws English', () => {
  const app = source('App.jsx');
  assert.match(app, /const chooseEnglish = async \(\) => \{\n\s*const save = \(\) => saveSettings\(\{ 'ui\.language': 'en' \}\)\.catch\(\(\) => null\);\n\s*if \(!await save\(\)\) await save\(\);\n\s*await languages\.want\('en'\);\n\s*\};/);
});

test('at launch only the setting\'s language is asked for, the system\'s only when the setting is not read', () => {
  const app = source('App.jsx');
  const load = app.slice(app.indexOf('const load = useCallback('), app.indexOf('}, [languages]);'));
  const reads = load.split('\n').filter((line) => line.includes('languages.want(resolveLanguage(settings'));
  assert.equal(reads.length, 2); // the limited app's screens, and the setup or the window
  assert.ok(reads.every((line) => line.includes('await languages.want(')));
  // each is drawn before the screen it leads to
  for (const line of reads) assert.ok(load.indexOf(line) < load.indexOf('setState({ phase: ', load.indexOf(line)));
  // the settings read is kept when the setup read fails, so the error screen is in the setting's language
  assert.match(load, /await Promise\.allSettled\(\[get\('\/api\/setup'\), get\('\/api\/settings'\)\]\);\n\s*if \(settings\.status === 'fulfilled'\) await languages\.want\(/);
  const system = [...app.matchAll(/^.*resolveLanguage\('system'.*$/gm)].map(([line]) => line.trim());
  assert.deepEqual(system, ["await languages.fallback(resolveLanguage('system', navigator.language)); // the setting was not read"]);
  assert.ok(load.indexOf('} catch (error) {') < load.indexOf('languages.fallback('));
  assert.doesNotMatch(app, /useEffect\(\(\) => \{\n\s*(setLanguage|languages\.want)\(/);
});

test('a change in Settings loads the language first and saves it only once loaded; a failure says so and saves nothing', () => {
  const settings = source('components/Settings.jsx');
  const change = settings.match(/onChange=\{async \(value\) => \{\n[\s\S]*?\n\s*\}\} \/>/)?.[0] ?? '';
  const loads = change.indexOf('await loadLanguage(resolveLanguage(value, navigator.language))');
  const fails = change.indexOf("setProblem('language_not_loaded');\n");
  const returns = change.indexOf('return;', fails);
  const saves = change.indexOf("await personal.save({ 'ui.language': value })");
  assert.ok(loads > 0 && loads < fails && fails < returns && returns < saves, change);
  assert.match(change, /\) onLanguage\(value\);/);
  assert.ok(en['errors.language_not_loaded'] && !HAN.test(en['errors.language_not_loaded']));
  assert.match(zhCN['errors.language_not_loaded'], HAN);
  assert.match(en['errors.language_not_loaded'], /Reload the window/); // a load that failed stays failed until then
});
