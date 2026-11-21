// Design review Z: every holder that outlives the views (a module's own Map, Set, object, list or `let`) says how the
// deletion of a project or a conversation reaches what it holds. A view's own state needs no entry: a deletion known to
// have happened leaves the view (Shell.jsx), and the deletion scenarios in tests/window_answers.test.mjs hold what a
// view shows meanwhile. A new module-level holder fails here until it is listed with its rule; a listed one that no
// longer exists fails too. This is a declaration check, in the manner of fields.test.mjs: it makes a holder say its
// rule; it does not prove the rule.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const IDS = 'ids, counts or flags only: nothing of a record';
const HELD = {
  // of a conversation
  'live.js: turns': 'a conversation\'s live turn (its message and answer): forget() drops it once the deletion is known; none is taken in after',
  'live.js: unsaved': 'answers shown but not saved, by run: forget() drops its conversation\'s',
  'live.js: unsavedIn': IDS,
  'live.js: gone': IDS,
  'live.js: aborts': 'a conversation\'s open stream: forget() closes it',
  'live.js: routes': IDS, 'live.js: halted': IDS, 'live.js: stops': IDS, 'live.js: followed': IDS, 'live.js: edited': IDS,
  'live.js: listeners': 'functions', 'live.js: shown': IDS,
  'components/Ask.jsx: typed': 'the researcher\'s own unsent words, by question id (design review X, rider A, Harold 2026-11-21): never shown again once its question is gone; kept until the window closes',
  // of a project
  'library.js: chosen': 'the papers chosen for the next message: their titles go at every change (libraryChanged), their ids stay until sent or another project\'s are chosen; only the open project\'s are shown',
  'library.js: opening': IDS,
  'library.js: generation': IDS, 'library.js: retitling': IDS, 'library.js: latestActions': IDS, 'library.js: actions': IDS,
  'library.js: uploads': 'the uploads\' queue: a promise, no record', 'library.js: waiting': IDS,
  'memory.js: dismissed': IDS,
  'memory.js: kept': 'what a memory action could not save, by view and project: the error in words, its code and the record\'s id, never its text (MB1)',
  'settings.js: listings': 'each provider\'s models, by provider and project, with whether the project allows each: no record of the project; forgotten when a provider or a protection changes',
  // of neither
  'api.js: session': 'this launch\'s session', 'api.js: saving': 'the settings saves\' queue: a promise',
  'settings.js: catalogListeners': 'functions',
  'components/ModelPicker.jsx: listeners': 'functions', 'components/ModelPicker.jsx: choice': 'the personal model choice',
  'components/ModelPicker.jsx: reading': 'a promise of the personal settings', 'components/ModelPicker.jsx: picks': IDS,
  'components/Shell.jsx: NONE': 'an empty list, never written', 'library.js: NONE': 'an empty list, never written',
  'i18n/index.js: loading': 'the catalogs being loaded',
};

const HOLDER = /^(?:export )?(?:(?:const|let) (\w+) = (?:new (?:Map|Set|WeakMap)\(\)|\{\}|\[\])|let (\w+)\b)/;
function files(dir, under = '') {
  return readdirSync(join(dir, under), { withFileTypes: true }).flatMap((entry) => (entry.isDirectory()
    ? (under === 'components' && entry.name === 'ui' ? [] : files(dir, join(under, entry.name)))
    : /\.jsx?$/.test(entry.name) ? [join(under, entry.name)] : []));
}

test('every holder that outlives the views says how a deletion reaches it', () => {
  const found = [];
  for (const file of files(SRC)) {
    for (const line of readFileSync(join(SRC, file), 'utf8').split('\n')) {
      const match = HOLDER.exec(line);
      if (match) found.push(`${file}: ${match[1] ?? match[2]}`);
    }
  }
  assert.deepEqual(found.filter((key) => !HELD[key]), [], 'module-level holders with no rule for a deletion');
  assert.deepEqual(Object.keys(HELD).filter((key) => !found.includes(key)), [], 'listed holders that no longer exist');
});
