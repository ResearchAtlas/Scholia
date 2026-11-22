// Design review X: every text field in the window says which rule keeps what is typed in it from an answer. Either its
// tag makes it read-only (readOnly= or disabled=) while its request is under way, or it is listed below with the reason
// no answer replaces, clears or closes it over newer text. A new text field fails here until it does one or the other.
// Design review AF: a component that draws a text field for others (CommitField, FolderField, Passphrase) is a field at
// each place it is used, since each form decides whether its answer closes it; and a form whose field goes read-only is
// closed, and its fields written, only by its answer: its other buttons are busy with it (design review U's rule C).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'components');
const COMMITTED = 'CommitField: its commit\'s answer leaves it drawn, and a saved value replaces its text only if nothing was typed since';
// file: the field's id as written (or its tag's first attribute) -> why no answer changes it over newer text
const OTHER_RULE = {
  'ConversationView.jsx: composer': 'the message box: a failed send is given back in front of what was typed since (Composer.submit)',
  'Backups.jsx: {id}': 'Passphrase\'s own field: each use says its rule',
  'Governance.jsx: {`${name}-venue`}': 'HoldsChoice: inside its fieldset, disabled by the form that draws it while its request is under way',
  'Governance.jsx: allowlist-add': 'cleared after its add only if it still holds what was added',
  'Providers.jsx: {`key-${provider.name}`}': 'cleared after its save only if it still holds the key saved; its card is the same card in every group',
  'Backups.jsx: restore-file': 'no request answers into it: Restore opens the confirmation',
  'Library.jsx: {id}': 'the search field: results never write it',
  'Providers.jsx: {`pick-${provider.name}`}': 'a filter: nothing is sent',
  'Providers.jsx: {`per-model-${provider.name}`}': 'a filter: nothing is sent',
  'Settings.jsx: subagent-add': 'cleared in the press that adds it, never by an answer; nothing is added, or cleared, while a change is saved',
  // each use of a shared field
  'Backups.jsx: <FolderField label={t(\'folder.label\')} v': 'a full backup\'s folder: its form stays; only the researcher\'s own choice in the folder dialog replaces it',
  'ProjectExport.jsx: <FolderField label={t(\'folder.label\')} v': 'an export\'s folder: its form stays; only the researcher\'s own choice in the folder dialog replaces it',
  'Backups.jsx: full-backup-passphrase': 'given back after a refused start unless another was typed since; its form stays',
  'Backups.jsx: restore-passphrase': 'no request answers into it: Restore opens the confirmation',
  'ProjectExport.jsx: export-passphrase': 'given back after a refused start unless another was typed since; sent only while it is shown',
  'Governance.jsx: project-review-venue': COMMITTED,
  'Providers.jsx: {`window-${provider.name}`}': COMMITTED,
  'Providers.jsx: {`w-${provider.name}-${m.id}`}': COMMITTED,
  'Settings.jsx: conversation-budget': COMMITTED,
  'Settings.jsx: subagents-at-once': COMMITTED,
  'Settings.jsx: subagents-tool-calls': COMMITTED,
  'Settings.jsx: subagents-effort-cap': COMMITTED,
  'Settings.jsx: project-name': COMMITTED,
  'Settings.jsx: project-venue': COMMITTED,
  'Settings.jsx: project-citation-style': COMMITTED,
  'Settings.jsx: project-budget': COMMITTED,
  'Settings.jsx: {`${prefix}-${name}`}': COMMITTED,
  'Settings.jsx: {`role-${role}`}': COMMITTED,
};
// file: a button's label -> why it may be pressed while its form's request is under way
const NOT_BUSY = {
  'Memory.jsx: memory.cancel': 'closes an editor the answer\'s reread closes too; Edit is busy, so none opens before the answer',
  // around the form, in the component that draws it (recheck r9)
  'Memory.jsx: Row <Button> memory.cancel': 'the same Cancel, inside its form (above)',
  'Memory.jsx: Proposal <Button> memory.cancel': 'the same Cancel, inside its form (above)',
  'Sidebar.jsx: NewProjectDialog <Dialog>': 'its onOpenChange refuses to close while its request is under way',
  'Sidebar.jsx: RenameDialog <Dialog>': 'its onOpenChange refuses to close while its request is under way',
  'ModelPicker.jsx: ModelPicker <Popover>': 'its onOpenChange refuses to close while a window is saved',
};

// every opening tag of these names, whole, with its line
function tags(source, names) {
  const found = [];
  for (const start of source.matchAll(new RegExp(`<(?:${names})\\b`, 'g'))) {
    let depth = 0; let end = start.index;
    for (; end < source.length; end += 1) {
      const c = source[end];
      if (c === '{') depth += 1; else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    found.push({ tag: source.slice(start.index, end + 1), at: start.index, after: source.slice(end + 1, end + 120),
      line: source.slice(0, start.index).split('\n').length });
  }
  return found;
}
// A shared field component (one that draws a text field for others) is named here, so each use is checked: a new one
// that forwards readOnly to its Input passes at its definition, and must be added (the batch I review).
const FIELDS = 'Input|Textarea|textarea|input|CommitField|FolderField|Passphrase';
const files = () => readdirSync(DIR).filter((name) => name.endsWith('.jsx')).map((file) => [file, readFileSync(join(DIR, file), 'utf8')]);
const text = (tag) => !/type="(file|checkbox|radio|range)"/.test(tag);
const locked = (tag) => /\b(readOnly|disabled)=/.test(tag);

test('every text field keeps what is typed in it from an answer, by one of the two rules', () => {
  const unruled = []; const used = new Set();
  for (const [file, source] of files()) {
    for (const { tag, line } of tags(source, FIELDS)) {
      if (!text(tag)) continue;
      const id = /\bid=("([^"]*)"|\{[^\n]*?\}(?=[\s/>]))/.exec(tag);
      const key = `${file}: ${id ? id[2] ?? id[1] : tag.slice(0, 40)}`;
      if (locked(tag)) continue;
      if (OTHER_RULE[key]) { used.add(key); continue; }
      unruled.push(`${file}:${line} ${key}`);
    }
  }
  assert.deepEqual(unruled, [], 'text fields that neither go read-only while their request is under way nor are listed with their rule');
  assert.deepEqual(Object.keys(OTHER_RULE).filter((key) => !used.has(key)), [], 'listed fields that no longer exist');
});

test('a form whose field goes read-only while its request is under way is closed only by its answer', () => {
  const pressable = []; const used = new Set();
  for (const [file, source] of files()) {
    for (const open of source.matchAll(/<form\b/g)) {
      const form = source.slice(open.index, source.indexOf('</form>', open.index));
      if (!tags(form, `${FIELDS}|HoldsChoice`).some(({ tag }) => text(tag) && locked(tag))) continue;
      for (const { tag, after, at } of tags(form, 'Button|button')) {
        if (/\bdisabled=(?!\{false\})/.test(tag)) continue; // the batch I review: a plain button counts, and disabled={false} is not busy
        const key = `${file}: ${/t\('([^']+)'/.exec(after)?.[1] ?? tag.slice(0, 40)}`;
        if (NOT_BUSY[key]) { used.add(key); continue; }
        pressable.push(`${file}:${source.slice(0, open.index + at).split('\n').length} ${key}`);
      }
    }
  }
  assert.deepEqual(pressable, [], 'buttons that can be pressed while their form\'s request is under way');
  assert.deepEqual(Object.keys(NOT_BUSY).filter((key) => !/ <\w/.test(key) && !used.has(key)), [], 'listed buttons that no longer exist');
});

// Recheck r9: the controls around such a form count too. In the component that draws it, every other text field is
// read-only with it (a filter included: the answer may clear it), and every control that can close the form or write
// its fields (onClick, onSelect, onPick, onOpenChange) is busy, or listed. A close control drawn by another component
// (the import's toggle is LocalHelperSection's) is not seen here: its scenario holds it.
test('a form whose field goes read-only is closed only by its answer, whatever around it could close it', () => {
  const live = []; const used = new Set(Object.keys(NOT_BUSY).filter((key) => !/ <\w/.test(key)));
  for (const [file, source] of files()) {
    const starts = [...source.matchAll(/^(?:export )?function \w+\(/gm)].map((m) => m.index);
    const seen = new Set();
    for (const open of source.matchAll(/<form\b/g)) {
      const form = source.slice(open.index, source.indexOf('</form>', open.index));
      if (!tags(form, `${FIELDS}|HoldsChoice`).some(({ tag }) => text(tag) && locked(tag))) continue;
      const from = Math.max(...starts.filter((at) => at < open.index));
      if (seen.has(from)) continue;
      seen.add(from);
      const part = source.slice(from, starts.find((at) => at > open.index) ?? source.length);
      const name = /function (\w+)/.exec(part)[1];
      for (const { tag } of tags(part, FIELDS)) if (text(tag) && !locked(tag)) live.push(`${file}: ${name} ${tag.slice(0, 50)}`);
      for (const { tag, after } of tags(part, '[A-Za-z][\\w.]*')) {
        if (!/\bon(Click|Select|Pick|OpenChange)=/.test(tag) || /\bdisabled=(?!\{false\})/.test(tag)) continue;
        const type = /^<([\w.]+)/.exec(tag)[1];
        const label = /^(Button|button)$/.test(type) && /t\('([^']+)'/.exec(after)?.[1];
        const key = `${file}: ${name} <${type}>${label ? ` ${label}` : ''}`;
        if (NOT_BUSY[key]) { used.add(key); continue; }
        live.push(key);
      }
    }
  }
  assert.deepEqual(live, [], 'fields or controls around a form that can change it while its request is under way');
  assert.deepEqual(Object.keys(NOT_BUSY).filter((key) => !used.has(key)), [], 'listed controls that no longer exist');
});
