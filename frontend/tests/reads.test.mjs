// Design review AG: every place that asks the backend for something to show says why an answer asked for one thing (a
// project, a scope, a conversation, a page) is never shown, continued or said under another. Three rules close it by
// construction: HOOK (read through libraryRead.js: shown only under its key and generation, and a reload kept from another
// key reads nothing), KEYED (a component drawn with a React key made of everything its requests depend on, so another
// key is another reader and the old one's answers go nowhere), and NONE (it asks for nothing that can change under it).
// Anything else says its own guard in words. A new read fails here until it is listed; a listed one that is gone fails
// too; a KEYED component drawn anywhere without a key fails. This is a declaration check, in the manner of
// holders.test.mjs: it makes a read say its rule; the scenarios named beside a rule hold it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const HOOK = 'HOOK';
const NONE = 'NONE';
const KEYED = (component, by) => ({ component, by });
const READS = {
  'App.jsx: App': NONE,
  'api.js: saveNow': 'a save\'s own read of the hash it saves against, in the press that saves',
  'components/Backups.jsx: BackupsSection': NONE,
  'components/ConversationView.jsx: ConversationView': KEYED('ConversationView', 'the visit (Shell.jsx); within a visit only the newest read asked in the current papers generation is shown (newest, readGuard)'),
  'components/ConversationView.jsx: useMention': HOOK,
  'components/FollowUp.jsx: BudgetBar': 'in the conversation\'s view (KEYED by the visit); each read is its effect\'s, dropped once the effect is',
  'components/Governance.jsx: PrivateAllowlist': NONE,
  'components/Governance.jsx: AuditPages': KEYED('AuditPages', 'the log shown and each change to it (AuditLog); tests/ag scenarios'),
  'components/Library.jsx: useLibrary': HOOK,
  'components/LocalHelper.jsx: useHelperStatus': NONE,
  'components/ModelPicker.jsx: readChoice': NONE,
  'components/ModelPicker.jsx: useCatalog': 'in the conversation\'s view (KEYED by the visit); only the newest load of a picker still drawn is shown',
  'components/Providers.jsx: Providers': NONE,
  'components/Settings.jsx: useOfferedModels': NONE,
  'components/Settings.jsx: BackgroundRuns': NONE,
  'components/Shell.jsx: Shell': 'the two lists: HOOK; the project\'s record: by its id, one chain per project; its panel setting: decides only what is not decided, in the visit it was asked in (design review U)',
  'components/Trace.jsx: Attempt': 'a press on one attempt of one turn (rows keyed by run), shown in that row',
  'followup.js: followUpMode': NONE,
  'library.js: retitleChosen': 'applied only in the generation it was asked in, to the same project\'s chosen papers',
  'library.js: readAsks': 'only the newest read asked is shown (newest); what is shown is cleared when the conversation changes',
  'library.js: passageStretch': 'each stretch its own reader (KEYED by the paper, then by its index), dropped with its effect',
  'library.js: pagePart': 'each page its own reader (KEYED by the paper, then by its number), dropped with its effect',
  'library.js: pageImage': 'as pagePart',
  'live.js: recheck': 'by conversation id, in the module\'s own store (holders.test.mjs)',
  'live.js: drain': 'by conversation id, in the module\'s own store (holders.test.mjs)',
  'memory.js: readLeftOff': 'in the conversation\'s view (KEYED by the visit); what is shown is dropped when the project changes (readGuard)',
  'memory.js: readMemory': KEYED('MemoryViews', 'the project (Memory.jsx MemorySection), which draws none while the open project\'s record is not known'),
  'runs.js: followRun': 'a run\'s id, followed by the action that started it while its form is drawn',
  'search.js: indexStatus': 'the Library\'s: only the newest read, cleared when the project changes, in a Library KEYED by project; This project\'s: KEYED by project (ThisProject)',
  'settings.js: useSettingsFile': KEYED('ThisProject', 'the project, where a project is named; elsewhere the personal file (NONE)'),
  'settings.js: useInstructions': 'This project\'s: KEYED by project (ThisProject); the personal file\'s size with the open project\'s: only the newest read is shown',
  'settings.js: loadModels': 'a listing by provider and project in the module\'s own store (holders.test.mjs); each reader keeps only its newest read',
  'trace.js: readTrace': 'a turn\'s Details (rows keyed by run): its effect\'s read, dropped with it and at every papers change',
  'zotero.js: zoteroStatus': NONE,
  'zotero.js: zoteroCollections': 'its effect\'s read, dropped when the library changes',
  'zotero.js: zoteroItems': 'a page is added only to the list it was asked for (appendPage: library, collection, generation, cursor)',
};

function files(dir, under = '') {
  return readdirSync(join(dir, under), { withFileTypes: true }).flatMap((entry) => (entry.isDirectory()
    ? (under === 'components' && entry.name === 'ui' ? [] : files(dir, join(under, entry.name)))
    : /\.jsx?$/.test(entry.name) ? [join(under, entry.name)] : []));
}
const TOP = /^(?:export )?(?:async )?function (\w+)|^(?:export )?const (\w+) = /;
const READ = /(?<![.\w])(?:get|getBlob)\(|\bread = get\b/; // a GET asked, or handed on as a reader (trace.js)

test('every read says why another key\'s answer is not shown under it', () => {
  const found = new Set();
  const sources = Object.fromEntries(files(SRC).map((file) => [file, readFileSync(join(SRC, file), 'utf8')]));
  for (const [file, text] of Object.entries(sources)) {
    let top = '(module)';
    for (const line of text.split('\n')) {
      const match = TOP.exec(line);
      if (match) top = match[1] ?? match[2];
      if (/^\s*\/\//.test(line) || (file === 'api.js' && top !== 'saveNow')) continue;
      if (READ.test(line)) found.add(`${file}: ${top}`);
    }
  }
  assert.deepEqual([...found].sort(), Object.keys(READS).sort());
  for (const rule of Object.values(READS)) {
    if (typeof rule !== 'object') continue;
    const drawn = Object.values(sources).flatMap((text) => text.match(new RegExp(`<${rule.component}\\b[^>]*`, 'g')) ?? []);
    assert.ok(drawn.length > 0, `${rule.component} is drawn nowhere`);
    for (const use of drawn) assert.match(use, /\bkey=/, `${rule.component} is drawn without a key: ${use}`);
  }
});
