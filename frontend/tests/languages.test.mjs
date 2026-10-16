// The interface languages' catalogs (i18n/index.js, App.jsx, Settings): English is loaded with the
// window, Chinese is a part of its own loaded only at launch when it is the window's language, which
// is decided once there ("system" resolved from the system's locale). A change in Settings is saved
// and applies when the window reloads (Reload now). A catalog that cannot load at launch is said so
// in its own language (NOT_LOADED), with Try again and Use English, never as English; Use English
// that cannot be saved draws English for the window, which says so.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { LANGUAGES, NOT_LOADED, chooseEnglish, languageLoaded, loadLanguage, makeT } from '../src/i18n/index.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };

const SRC = new URL('../src/', import.meta.url);
const source = (path) => readFileSync(new URL(path, SRC), 'utf8');
const HAN = /\p{Script=Han}/u;
const launch = (app) => app.slice(app.indexOf('const launchLanguage = useCallback('), app.indexOf('}, [launchLanguage]);'));

test('the Chinese catalog loads when Chinese is asked for, and not otherwise', async () => {
  assert.deepEqual(LANGUAGES, ['en', 'zh-CN']); // listed without loading it
  assert.equal(languageLoaded('en'), true);
  assert.equal(languageLoaded('zh-CN'), false);
  await loadLanguage('en');
  assert.equal(languageLoaded('zh-CN'), false);
  await loadLanguage('zh-CN');
  assert.equal(languageLoaded('zh-CN'), true);
  assert.equal(makeT('zh-CN')('sidebar.projects'), '项目');
});

test('a catalog is loaded only at launch: saving a language in Settings does not change the drawn one until reload', () => {
  // the only load of a catalog in the interface is the launch's, which decides the window's language once
  const loads = readdirSync(SRC, { recursive: true }).filter((name) => /\.jsx?$/.test(name))
    .filter((name) => /\bloadLanguage\(/.test(source(name)));
  assert.deepEqual(loads.sort(), ['App.jsx', 'i18n/index.js']);
  const app = source('App.jsx');
  assert.equal([...app.matchAll(/\bloadLanguage\(/g)].length, 1);
  assert.match(app, /const launchLanguage = useCallback\(async \(setting\) => \{\n\s*if \(decided\.current\) return;\n\s*decided\.current = true;/);
  // the drawn language changes only there and in Use English that could not be saved
  assert.equal([...app.matchAll(/setShown\(/g)].length, 3);
  assert.match(app, /draw: \(\) => \{ setEnglishUnsaved\(true\); setShown\(\{ language: 'en', failed: null \}\); \}/);
  // a language saved in Settings only clears the not-saved notice
  assert.match(app, /onLanguage=\{\(\) => setEnglishUnsaved\(false\)\} \/>\}/);
  const settings = source('components/Settings.jsx');
  assert.match(settings, /onChange=\{async \(value\) => \{ if \(await personal\.save\(\{ 'ui\.language': value \}\)\) onLanguage\(value\); \}\} \/>/);
  assert.doesNotMatch(settings, /loadLanguage|languageChooser/);
});

test('Settings shows the saved choice, and that it applies on reload, with Reload now, which reloads the window', () => {
  const settings = source('components/Settings.jsx');
  assert.match(settings, /<Segmented label=\{t\('settings\.language'\)\} value=\{values\.ui\.language\}/);
  assert.match(settings, /\{resolveLanguage\(values\.ui\.language, navigator\.language\) !== drawn && \(\n\s*<div role="status"[^>]*>\n\s*<p>\{t\('settings\.languageOnReload'\)\}<\/p>\n\s*<Button[^>]*onClick=\{\(\) => window\.location\.reload\(\)\}>\{t\('settings\.reloadNow'\)\}<\/Button>/);
  assert.match(settings, /const drawn = useContext\(LanguageContext\);/);
  assert.equal(en['settings.reloadNow'], 'Reload now');
  assert.match(en['settings.languageOnReload'], /when the window reloads/);
  assert.match(zhCN['settings.languageOnReload'], HAN);
  assert.ok(!('errors.language_not_loaded' in en) && !('errors.language_not_loaded' in zhCN)); // Settings loads no catalog
});

test('at launch "system" resolves from the system\'s locale, and only the setting\'s language is asked for', () => {
  const app = source('App.jsx');
  assert.match(launch(app), /const chosen = resolveLanguage\(setting, navigator\.language\);\n\s*await loadLanguage\(chosen\)/);
  const load = app.slice(app.indexOf('const load = useCallback('), app.indexOf('}, [launchLanguage]);'));
  const reads = load.split('\n').filter((line) => line.includes('launchLanguage(settings'));
  assert.equal(reads.length, 2); // the limited app's screens, and the setup or the window
  assert.ok(reads.every((line) => line.includes("await launchLanguage(settings") && line.includes("?? 'system')")));
  for (const line of reads) assert.ok(load.indexOf(line) < load.indexOf('setState({ phase: ', load.indexOf(line)));
  assert.match(load, /await Promise\.allSettled\(\[get\('\/api\/setup'\), get\('\/api\/settings'\)\]\);\n\s*if \(settings\.status === 'fulfilled'\) await launchLanguage\(/);
  // the system's language only when the setting was not read, and only if none was decided yet
  assert.match(load, /\} catch \(error\) \{\n\s*await launchLanguage\('system'\); \/\/ the setting was not read/);
  assert.doesNotMatch(app, /languageSwitch|languages\.want|fallback\(/);
});

test('a launch that cannot load its language offers Try again and Use English, in that language', () => {
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
  assert.match(launch(app), /\(\) => setShown\(\{ language: null, failed: chosen \}\)/);
  assert.match(app, /if \(shown\.failed\) return <LanguageNotLoaded language=\{shown\.failed\} onEnglish=\{englishInstead\} \/>;/);
  assert.match(app, /const text = NOT_LOADED\[language\];/);
  assert.match(app, /onClick=\{\(\) => window\.location\.reload\(\)\}>\{text\.retry\}/);
  assert.match(app, /onClick=\{onEnglish\}>\{text\.english\}/);
  assert.match(app, /if \(!language\) return null;/);
});

test('Use English saves English, then reloads; once more if that did not take; drawn unsaved if it cannot be saved', async () => {
  const run = async (answers) => {
    const calls = [];
    await chooseEnglish({
      save: async () => { calls.push('save'); return answers.shift(); },
      reload: () => calls.push('reload'),
      draw: () => calls.push('draw'),
    });
    return calls;
  };
  assert.deepEqual(await run([true]), ['save', 'reload']);
  assert.deepEqual(await run([false, true]), ['save', 'save', 'reload']); // the settings changed meanwhile
  assert.deepEqual(await run([false, false]), ['save', 'save', 'draw']); // English for this window, said unsaved
});

test('Use English that cannot be saved draws English with the notice, whose Try again saves once more', () => {
  const app = source('App.jsx');
  assert.match(app, /const saveEnglish = \(\) => saveSettings\(\{ 'ui\.language': 'en' \}\)\.then\(Boolean, \(\) => false\);/);
  assert.match(app, /const englishInstead = \(\) => chooseEnglish\(\{ save: saveEnglish, reload: \(\) => window\.location\.reload\(\),\n\s*draw: \(\) => \{ setEnglishUnsaved\(true\); setShown\(\{ language: 'en', failed: null \}\); \} \}\);/);
  assert.match(app, /const retryEnglish = async \(\) => \{ if \(await saveEnglish\(\)\) setEnglishUnsaved\(false\); \};/);
  assert.match(app, /<LanguageContext\.Provider value=\{language\}>\n\s*\{englishUnsaved && <EnglishNotSaved onRetry=\{retryEnglish\} onClose=\{\(\) => setEnglishUnsaved\(false\)\} \/>\}/);
  assert.match(app, /role="alert" className="fixed inset-x-4 top-4 /);
  assert.match(app, /\{t\('errors\.english_not_saved'\)\}<\/p>\n\s*<div className="flex gap-2">\n\s*<Button[^>]*onClick=\{onRetry\}>\{t\('common\.retry'\)\}<\/Button>\n\s*<Button[^>]*onClick=\{onClose\}>\{t\('common\.close'\)\}/);
  assert.match(en['errors.english_not_saved'], /could not be saved[\s\S]*Try again saves it once more/);
});
