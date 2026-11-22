// Design review Z: every holder that outlives the views (a module's own Map, Set, object, list or `let`) says how the
// deletion of a project or a conversation reaches what it holds. A view's own state needs no entry: a deletion known to
// have happened leaves the view (Shell.jsx), and the deletion scenarios in tests/window_answers.test.mjs hold what a
// view shows meanwhile. A new module-level holder fails here until it is listed with its rule; a listed one that no
// longer exists fails too. This is a declaration check, in the manner of fields.test.mjs: it makes a holder say its
// rule; it does not prove the rule.
// Design review AB: a restore replaces the database under every holder here and under every view. However a restore
// that succeeded is left, the window reloads (Backups.jsx; tests/restore_exits.test.mjs), so none of them outlives one,
// and none needs a rule of its own for a restore. What outlives a reload is the browser's storage: each use of it says
// below how a deletion and a restore reach what it keeps.
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
  'live.js: gone': 'ids of conversations known deleted in this window; a restore, which can bring one back, reloads the window (design review AB)',
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

const STORED = {
  'api.js: sessionStorage': 'this launch\'s session (session.js): nothing of the database; a restore does not change it',
  'components/Shell.jsx: localStorage': 'the id of the project last open: opened again only if the projects read after the reload list it',
};
const STORES = ['localStorage', 'sessionStorage', 'indexedDB', 'caches', 'cookie'];

test('everything that outlives a reload says how a deletion and a restore reach it', () => {
  const found = [];
  for (const file of files(SRC)) {
    const code = readFileSync(join(SRC, file), 'utf8').replace(/(^|\s)\/\/.*$/gm, '$1'); // a comment, not 'http://'
    for (const store of STORES) if (new RegExp(`\\b${store}\\b`).test(code)) found.push(`${file}: ${store}`);
  }
  assert.deepEqual(found.filter((key) => !STORED[key]), [], 'browser storage with no rule for a deletion and a restore');
  assert.deepEqual(Object.keys(STORED).filter((key) => !found.includes(key)), [], 'listed storage that is no longer used');
});

// Design review AD: a 404 taken as a deletion is knowledge, and knowledge has one landing place (Shell.jsx left()): a
// conversation's through live.js forget(), which says 'deleted' once and which the Shell hears; a project's through the
// Shell's gone(); a refusal that only suggests the project went asks for it. A new place that reads a 404 as a deletion
// fails here until it hands it on in the same statement. A declaration check, as the two above.
// From a test to the end of what it governs: its statement's `;`, or the block it opens.
const through = (code, start) => {
  let depth = 0; let opened = false;
  for (let i = start; i < code.length; i += 1) {
    const c = code[i];
    if (c === '{') { depth += 1; opened = true; } else if (c === '}') { depth -= 1; if (depth <= 0) return code.slice(start, i + 1); } // its block's end, or its enclosing one's
    else if (c === ';' && depth === 0) return code.slice(start, i + 1);
  }
  return code.slice(start);
};

test('every 404 taken as a deletion is handed to where knowledge lands', () => {
  // The batch H review: by the statement, not the line (what a test of deleted(...) governs, across lines), and a 404
  // read directly, not through deleted(), is listed with why it is not a deletion's knowledge.
  const RAW_404 = {
    'live.js': 'deleted() itself, and send()\'s refusal of a conversation forgotten here',
    'settings.js': 'a provider no longer set up lists no models: not a project\'s or a conversation\'s deletion',
  };
  const stray = []; const raw = new Set();
  for (const file of files(SRC)) {
    const code = readFileSync(join(SRC, file), 'utf8').replace(/(^|\s)\/\/.*$/gm, '$1');
    for (const match of code.matchAll(/\bdeleted\(/g)) {
      if (code.slice(Math.max(0, match.index - 6), match.index) === 'const ') continue; // its definition
      const statement = through(code, match.index);
      if (!/\b(forget|gone)\(|\bonNoProject\?\.\(\)/.test(statement)) stray.push(`${file}: ${statement.split('\n')[0].trim()}`);
    }
    if (/\b(status\s*===?\s*404|404\s*===?\s*\w*\.?status)\b|ApiError\(404/.test(code)) raw.add(file);
  }
  assert.deepEqual(stray, [], 'a 404 taken as a deletion and kept to itself');
  assert.deepEqual([...raw].filter((file) => !RAW_404[file]).sort(), [], 'a 404 read directly, not through deleted()');
  assert.deepEqual(Object.keys(RAW_404).filter((file) => !raw.has(file)), [], 'listed 404 readers that no longer read one');
});

// Design review AE: what is open changes in one place, which refuses what is known deleted (Shell.jsx setOpen): the
// state's own setter is called there and nowhere else; and the conversation's view holds no conversation of its own (the
// one a first message makes is the Shell's from when it is made: rider L). A declaration check, as the ones above.
test('what is open changes in one place, and no view holds a conversation of its own', () => {
  const shell = readFileSync(join(SRC, 'components/Shell.jsx'), 'utf8');
  assert.match(shell, /const \[open, setRaw\] = useState\(/, 'the state of what is open');
  // setRaw named twice only: where it is made, and its one call, inside setOpen (no alias, no other call: the review, F2)
  assert.equal(shell.match(/\bsetRaw\b/g)?.length, 2, 'what is open is set outside setOpen');
  assert.match(shell, /const setOpen = useCallback\(\(change\) => setRaw\(\(now\) => \{/, 'its one call is setOpen\'s');
  const view = readFileSync(join(SRC, 'components/ConversationView.jsx'), 'utf8');
  assert.match(view, /\n {2}const id = conversationId;\n/, 'the view takes its conversation from the Shell alone');
});
