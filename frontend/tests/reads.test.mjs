// Design review AG: every place that asks the backend for something to show says why an answer asked for one thing (a
// project, a scope, a conversation, a page) is never shown, continued or said under another. Three rules close it by
// construction: HOOK (read through libraryRead.js: shown only under its key and generation, and a reload kept from another
// key reads nothing), KEYED (a component drawn with a React key made of everything its requests depend on, so another
// key is another reader and the old one's answers go nowhere), and NONE (it asks for nothing that can change under it).
// Anything else says its own guard in words. A new read fails here until it is listed; a listed one that is gone fails
// too; a KEYED component drawn anywhere without a key fails. This is a declaration check, in the manner of
// holders.test.mjs: it makes a read say its rule; the scenarios named beside a rule hold it. What it does not see (the
// batch K review): a second read inside a function already listed (one entry per top-level function, not per call); a
// read written as anything but get( or getBlob( (api.get, a POST used as a read, stream(, api(); what a key names (only
// that one is there: the scenarios hold what it must name); a key written after an arrow function in the same tag.
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
  'settings.js: useInstructions': KEYED('ThisProject', 'the project, where a project is named; elsewhere the personal file (NONE)'),
  'settings.js: useInstructionsBeside': HOOK,
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

// Design review AH (rider R): a read callback that depends on something (a useCallback or useMemo with a dependency, in
// whose body a read is asked) can be kept by whoever it was handed to (a save under way, a timer, a listener) and asked
// again after what it depends on changed: it would then ask for the earlier thing. Each one found says why it cannot:
// KEYED (it is drawn for one key, so one kept from another key belongs to a reader that is gone), or its dependencies,
// named here, are not what it asks for (a guard, a counter, a callback). A read through the hook is not found here: its
// reload is the hook's (rule H). A new one, or a new dependency of a listed one, fails until it is looked at. What this
// does not see: a callback that is a plain function of its draw (asked in the press that drew it, or carrying what it
// asks for: ZoteroBrowser's more, AuditLog's export), a read asked through a function not named in READS or WRAPPED,
// and a callback not assigned to a const; it counts parentheses, so an unbalanced one in a string or a comment fails it
// (saying so) where the parentheses do not balance, and could mislead it where they still do.
const WRAPPED = ['readGuardedAsks', 'searchProject']; // functions that ask a read named in READS
const CALLBACKS = {
  'App.jsx: App: load': ['launchLanguage'],
  'components/ConversationView.jsx: useConversationAsks: load': KEYED('ConversationView', 'the visit; within it only null (not made yet: nothing is asked) then its id'),
  'components/ConversationView.jsx: ConversationView: load': KEYED('ConversationView', 'the visit; within it only null (not made yet: nothing is asked) then its id'),
  'components/Governance.jsx: AuditPages: fetchPage': KEYED('AuditPages', 'the log shown'),
  'components/LeftOff.jsx: LeftOff: load': KEYED('ConversationView', 'the visit: one project'),
  'components/Library.jsx: useIndex: load': KEYED('Library', 'the project'),
  'components/Memory.jsx: MemoryViews: load': KEYED('MemoryViews', 'the project'),
  'components/ModelPicker.jsx: useCatalog: load': KEYED('ConversationView', 'the visit: one project'),
  'components/Providers.jsx: PerModel: load': KEYED('ProviderCard', 'the provider'),
  'components/SearchIndex.jsx: IndexSection: load': KEYED('ThisProject', 'the project'),
  'components/Settings.jsx: BackgroundRuns: load': ['guard'],
  'components/Shell.jsx: Shell: left': ['removals', 'lost', 'setOpen'],
  'components/Shell.jsx: Shell: askProject': ['gone', 'lost'],
  'settings.js: useSettingsFile: saver': KEYED('ThisProject', 'the project, where one is named'),
  'settings.js: useInstructions: reload': KEYED('ThisProject', 'the project, where one is named: the personal file\'s size with the open project\'s is read through the hook'),
};

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
  const asks = new RegExp(`${READ.source}|(?<![.\\w])(?:${[...Object.keys(READS).map((key) => key.split(': ')[1]), ...WRAPPED].join('|')})\\(`);
  const kept = {};
  for (const [file, text] of Object.entries(sources)) {
    for (const made of text.matchAll(/const (\w+) = use(?:Callback|Memo)\(/g)) {
      let end = made.index + made[0].length;
      for (let depth = 1; depth > 0; end += 1) {
        assert.ok(end < text.length, `${file}: ${made[1]}: its parentheses do not balance (one in a string or a comment?)`);
        depth += { '(': 1, ')': -1 }[text[end]] ?? 0;
      }
      const call = text.slice(made.index + made[0].length, end - 1).split('\n').filter((line) => !/^\s*\/\//.test(line)).join('\n');
      const depends = /,\s*\[([^[\]]*)\]\s*$/.exec(call)?.[1].split(',').map((name) => name.trim()).filter(Boolean);
      assert.ok(depends, `${file}: ${made[1]} has no dependency list`);
      const top = text.slice(0, made.index).split('\n').map((line) => TOP.exec(line)).filter(Boolean).at(-1);
      if (depends.length && asks.test(call)) kept[`${file}: ${top[1] ?? top[2]}: ${made[1]}`] = depends;
    }
  }
  assert.deepEqual(Object.keys(kept).sort(), Object.keys(CALLBACKS).sort());
  for (const [name, rule] of Object.entries(CALLBACKS)) if (Array.isArray(rule)) assert.deepEqual(kept[name], rule, name);
  for (const rule of [...Object.values(READS), ...Object.values(CALLBACKS)]) {
    if (typeof rule !== 'object' || Array.isArray(rule)) continue;
    const drawn = Object.values(sources).flatMap((text) => text.match(new RegExp(`<${rule.component}\\b[^>]*`, 'g')) ?? []);
    assert.ok(drawn.length > 0, `${rule.component} is drawn nowhere`);
    for (const use of drawn) assert.match(use, /\bkey=/, `${rule.component} is drawn without a key: ${use}`);
  }
});
