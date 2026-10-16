// The interface languages' catalogs (i18n/index.js): English is loaded with the window, Chinese is a
// part of its own loaded only when it is asked for; the window is drawn in a language only once its
// catalog is here, so nothing shows in another language meanwhile. Only the setting's language is
// loaded at launch. A catalog that cannot load at launch is said so in its own language (NOT_LOADED),
// with Try again and Use English, never as English; a change in Settings loads first and saves after.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { LANGUAGES, NOT_LOADED, languageChooser, languageLoaded, languageSwitch, loadLanguage, makeT } from '../src/i18n/index.js';
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

test('Use English saves the setting as English (once more if that did not take), draws English, and says if it was not saved', () => {
  const app = source('App.jsx');
  assert.match(app, /const saveEnglish = async \(\) => \{\n\s*const save = \(\) => saveSettings\(\{ 'ui\.language': 'en' \}\)\.catch\(\(\) => null\);\n\s*setEnglishUnsaved\(!\(await save\(\) \|\| await save\(\)\)\);\n\s*\};/);
  assert.match(app, /const chooseEnglish = async \(\) => \{\n\s*await saveEnglish\(\);\n\s*await languages\.want\('en'\);\n\s*\};/);
  // over the window, in the language drawn (English), with Try again saving once more
  assert.match(app, /<LanguageContext\.Provider value=\{language\}>\n\s*\{englishUnsaved && <EnglishNotSaved onRetry=\{saveEnglish\} \/>\}/);
  assert.match(app, /\{t\('errors\.english_not_saved'\)\}<\/p>\n\s*<Button[^>]*onClick=\{onRetry\}>\{t\('common\.retry'\)\}<\/Button>/);
  assert.match(en['errors.english_not_saved'], /could not be saved[\s\S]*Try again saves it once more/);
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

// Settings' choices, as Settings makes them (languageChooser), each recorded in calls.
function chooser() {
  const { load, loads } = pending();
  const calls = [];
  const choose = languageChooser({ load, systemLocale: 'en-US' });
  const pick = (value, saved = true) => choose(value, {
    save: async (choice) => { calls.push(['save', choice]); return saved; },
    draw: (choice) => calls.push(['draw', choice]),
    failed: () => calls.push(['failed', value]),
  });
  return { pick, loads, calls };
}

test('in Settings an earlier language choice still loading saves and draws nothing once a later one is made', async () => {
  const { pick, loads, calls } = chooser();
  const chinese = pick('zh-CN'); // Chinese loads
  const system = pick('system'); // then System (English here) while it does
  assert.deepEqual(loads.map((entry) => entry.language), ['zh-CN', 'en']);
  assert.deepEqual(calls, []); // nothing saved before its catalog has loaded
  loads[1].resolve();
  await system;
  loads[0].resolve();
  await chinese;
  assert.deepEqual(calls, [['save', 'system'], ['draw', 'system']]);

  const failing = chooser();
  const first = failing.pick('zh-CN');
  const second = failing.pick('en');
  failing.loads[0].reject(new NotLoaded('refused')); // the earlier one's failure is not the latest: not said
  failing.loads[1].resolve();
  await Promise.all([first, second]);
  assert.deepEqual(failing.calls, [['save', 'en'], ['draw', 'en']]);
});

test('in Settings a language that cannot load saves nothing and says so; a save refused draws nothing', async () => {
  const { pick, loads, calls } = chooser();
  const choice = pick('zh-CN');
  loads[0].reject(new NotLoaded('refused'));
  await choice;
  assert.deepEqual(calls, [['failed', 'zh-CN']]);

  const refused = chooser();
  const unsaved = refused.pick('zh-CN', false);
  refused.loads[0].resolve();
  await unsaved;
  assert.deepEqual(refused.calls, [['save', 'zh-CN']]);
});

test('Settings chooses the language through languageChooser, and a failed one shows Try again, which reloads the window', () => {
  const settings = source('components/Settings.jsx');
  assert.match(settings, /const \[chooser\] = useState\(\(\) => languageChooser\(\)\);/);
  assert.match(settings, /chooser\(value, \{ save: \(choice\) => personal\.save\(\{ 'ui\.language': choice \}\), draw: onLanguage,\n\s*failed: \(\) => setLanguageFailed\(true\) \}\);/);
  assert.match(settings, /onChange=\{chooseLanguage\} \/>\n\s*\{languageFailed && <div className="mt-3"><LoadState problem="language_not_loaded" onRetry=\{\(\) => window\.location\.reload\(\)\} \/><\/div>\}/);
  assert.doesNotMatch(settings, /loadLanguage|personal\.save\(\{ 'ui\.language': value \}\)/); // no other way to the setting
  assert.match(en['errors.language_not_loaded'], /was not changed\. Try again reloads the window\.$/); // names the button
  assert.match(zhCN['errors.language_not_loaded'], /未更改。点“重试”会重新载入窗口。$/);
  assert.equal(en['common.retry'], 'Try again');
});
