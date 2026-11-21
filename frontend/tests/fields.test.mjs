// Design review X: every text field in the window says which rule keeps what is typed in it from an answer. Either its
// tag makes it read-only (readOnly= or disabled=) while its request is under way, or it is listed below with the reason
// no answer replaces, clears or closes it over newer text. A new text field fails here until it does one or the other.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'components');
// file: the field's id as written (or its tag's first attribute) -> why no answer changes it over newer text
const OTHER_RULE = {
  'ConversationView.jsx: composer': 'the message box: a failed send is given back in front of what was typed since (Composer.submit)',
  'fields.jsx: {id}': 'CommitField: a saved value replaces the text only if nothing was typed since the commit (edited)',
  'FolderField.jsx: {id}': 'a path: only the researcher\'s own choice in the folder dialog replaces it',
  'Governance.jsx: {`${name}-venue`}': 'HoldsChoice: inside its fieldset, disabled by the form that draws it while its request is under way',
  'Governance.jsx: allowlist-add': 'cleared after its add only if it still holds what was added',
  'Providers.jsx: {`key-${provider.name}`}': 'cleared after its save only if it still holds the key saved',
  'Backups.jsx: {id}': 'Passphrase: given back after a refused start unless another was typed since',
  'Backups.jsx: restore-file': 'no request answers into it: Restore opens the confirmation',
  'Library.jsx: {id}': 'the search field: results never write it',
  'ModelPicker.jsx: picker-search': 'a filter: nothing is sent',
  'Providers.jsx: {`pick-${provider.name}`}': 'a filter: nothing is sent',
  'Providers.jsx: {`per-model-${provider.name}`}': 'a filter: nothing is sent',
  'Settings.jsx: subagent-add': 'cleared in the press that adds it, never by an answer; nothing is added, or cleared, while a change is saved',
};

// every <Input, <Textarea, <textarea and <input opening tag, whole, with its line
function tags(source) {
  const found = [];
  for (const start of source.matchAll(/<(?:Input|Textarea|textarea|input)\b/g)) {
    let depth = 0; let end = start.index;
    for (; end < source.length; end += 1) {
      const c = source[end];
      if (c === '{') depth += 1; else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    found.push({ tag: source.slice(start.index, end + 1), line: source.slice(0, start.index).split('\n').length });
  }
  return found;
}

test('every text field keeps what is typed in it from an answer, by one of the two rules', () => {
  const unruled = []; const used = new Set();
  for (const file of readdirSync(DIR).filter((name) => name.endsWith('.jsx'))) {
    for (const { tag, line } of tags(readFileSync(join(DIR, file), 'utf8'))) {
      if (/type="(file|checkbox|radio|range)"/.test(tag)) continue; // not text
      const id = /\bid=("([^"]*)"|\{[^\n]*?\}(?=[\s/>]))/.exec(tag);
      const key = `${file}: ${id ? id[2] ?? id[1] : tag.slice(0, 40)}`;
      if (/\b(readOnly|disabled)=/.test(tag)) continue;
      if (OTHER_RULE[key]) { used.add(key); continue; }
      unruled.push(`${file}:${line} ${key}`);
    }
  }
  assert.deepEqual(unruled, [], 'text fields that neither go read-only while their request is under way nor are listed with their rule');
  assert.deepEqual(Object.keys(OTHER_RULE).filter((key) => !used.has(key)), [], 'listed fields that no longer exist');
});
