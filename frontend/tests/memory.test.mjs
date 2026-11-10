// S1-21a: memory as the window shows it (checklist H3, H4, U1, U2). What a turn's memory command did is read back from
// the record (the conversation's turns, or the backend's event after its commit), never the window's own guess; Where we
// left off shows once per return; its next-step button only fills the message box, sending and spending nothing.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';
import { makeT } from '../src/i18n/index.js';
import { actionLines, composerFill, dismissLeftOff, fillComposer, leftOffDismissed, recordText } from '../src/memory.js';
import { apply } from '../src/live.js';
import { libraryEvents } from '../src/library.js';
import { memoryChanged } from '../src/memory.js';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));
const t = makeT('en');

test('a turn\'s memory actions read back the records saved, and say the rest in words', () => {
  assert.deepEqual(actionLines(t, { action: 'saved', count: 1, records: [{ content: 'my method is interviews' }] }),
    ['Saved to memory: my method is interviews']);
  assert.deepEqual(actionLines(t, { action: 'saved', count: 1 }), ['Saved to memory']); // live, before the read
  assert.deepEqual(actionLines(t, { action: 'forgotten', count: 2 }), ['Forgot 2 memory records']);
  assert.deepEqual(actionLines(t, { action: 'memory_off', count: 0 }), [t('memory.memoryOff')]);
  assert.deepEqual(actionLines(t, { action: 'unknown' }), []);
  assert.deepEqual(actionLines(makeT('en'), { action: 'updated', records: [{ content: 'x' }] }), ['Updated in memory: x']);
});

test('the live turn takes a memory event only as the backend sends it, after its commit', () => {
  const turn = apply({ text: 'Remember: x' }, { type: 'memory', action: 'saved', count: 1 });
  assert.deepEqual(turn.memory, [{ action: 'saved', count: 1 }]);
});

test('progress is said in the interface\'s words, and other records as written', () => {
  assert.equal(recordText(t, { type: 'progress', content: 'Added 2 papers', structured: { event: 'materials_added', count: 2,
    date: '2026-10-22' } }), 'Added 2 papers to the library (2026-10-22)');
  assert.equal(recordText(t, { type: 'decision', content: 'We use interviews', structured: {} }), 'We use interviews');
});

test('Where we left off is shown once per return: dismissed until a newer turn', () => {
  assert.equal(leftOffDismissed('p', '2026-10-22T10:00:00.000Z'), false);
  dismissLeftOff('p', '2026-10-22T10:00:00.000Z');
  assert.equal(leftOffDismissed('p', '2026-10-22T10:00:00.000Z'), true);
  assert.equal(leftOffDismissed('p', '2026-10-22T15:00:00.000Z'), false);
  assert.equal(leftOffDismissed('q', '2026-10-22T10:00:00.000Z'), false);
});

test('the next-step button fills the message box and calls nothing else', () => {
  const filled = [];
  const listen = (event) => filled.push(event.detail);
  composerFill.addEventListener('fill', listen);
  fillComposer('Screen the 2020 papers');
  composerFill.removeEventListener('fill', listen);
  assert.deepEqual(filled, ['Screen the 2020 papers']);
  // In LeftOff.jsx, the button labeled leftOff.ask does fillComposer(...) and nothing more: no request, no send.
  const source = readFileSync(join(SRC, 'components/LeftOff.jsx'), 'utf8');
  const found = [];
  const rule = { create: (context) => ({
    JSXElement(element) {
      const label = element.children.find((c) => c.type === 'JSXExpressionContainer');
      if (!label || !context.sourceCode.getText(label).includes("'leftOff.ask'")) return;
      const click = element.openingElement.attributes.find((a) => a.name?.name === 'onClick');
      found.push(context.sourceCode.getText(click.value.expression));
    },
  }) };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { fill: rule } } }, rules: { 'check/fill': 'error' } }];
  assert.deepEqual(new Linter().verify(source, config, 'LeftOff.jsx').filter((m) => m.fatal), []);
  assert.deepEqual(found, ['() => fillComposer(found.next_step.content)']);
  // and the composer only sets its text from it (ConversationView.jsx), never sends
  const view = readFileSync(join(SRC, 'components/ConversationView.jsx'), 'utf8');
  assert.match(view, /const fill = \(event\) => \{ setText\(event\.detail\); box\.current\?\.focus\(\); \};/);
});

test('there is no separate review list: Things to confirm is in Where we left off and the memory views only', () => {
  const sources = readdirSync(join(SRC, 'components')).filter((name) => name.endsWith('.jsx'));
  const using = sources.filter((name) => readFileSync(join(SRC, 'components', name), 'utf8').includes('/api/memory/review')
    || readFileSync(join(SRC, 'components', name), 'utf8').includes('<Proposal'));
  assert.deepEqual(using.sort(), ['LeftOff.jsx', 'Memory.jsx']);
});

// Fix batch 1 (gpt-6-astra review 1, finding 7): no client-held derived state. Where we left off and the memory views
// drop what they show at once when the library or memory changes anywhere (a paper deleted, a note forgotten), read
// again under a guard, so an older read never restores it; the next-step button only fills the box with what is shown.
test('Where we left off and the memory views drop what they show when the library or memory changes', () => {
  for (const name of ['LeftOff.jsx', 'Memory.jsx', 'ConversationView.jsx']) { // the conversation reads records back too
    const source = readFileSync(join(SRC, 'components', name), 'utf8');
    assert.match(source, /libraryEvents\.addEventListener\('changed'/, name);
    assert.match(source, /readGuard\(\)/, name);
  }
  const live = readFileSync(join(SRC, 'live.js'), 'utf8');
  assert.match(live, /if \(event\.type === 'memory'\) memoryChanged\(/); // a turn's command or note changes memory
});

// Fix batch 2 (finding 5): every action that changes what the memory views show tells them: the project's memory
// switch, its protection (tightening or locking makes personal records its own) and a conversation's move.
test('the memory switch, a protection change and a move each emit a memory change', () => {
  const settings = readFileSync(join(SRC, 'components', 'Settings.jsx'), 'utf8');
  assert.match(settings, /own\.save\(\{ 'memory\.enabled': value === 'on' \}\)[^\n]*memoryChanged\(project\.id\)/);
  assert.match(readFileSync(join(SRC, 'components', 'Governance.jsx'), 'utf8'), /memoryChanged\(project\.id\)/);
  assert.match(readFileSync(join(SRC, 'components', 'Sidebar.jsx'), 'utf8'), /memoryChanged\(null\)/);
});

test('deleting a conversation tells the memory views (its turns\' records go with it)', () => {
  const sources = ['Sidebar.jsx', 'Shell.jsx'].map((name) => readFileSync(join(SRC, 'components', name), 'utf8')).join('\n');
  assert.match(sources, /delete[\s\S]{0,400}memoryChanged\(null\)/);
});

// Fix batch 4 (gpt-6-astra recheck 4, finding 3; Harold, 2026-10-21: no client-held derived state): one invalidation
// path. A memory change is the library's change, so everything that drops and reads again when a paper is deleted
// (the conversation's turns, its questions, Details, the Library, the memory views, Where we left off) does so when a
// record is saved, edited or forgotten; a change naming no project (null) reaches every project's views.
test('a memory change goes through the one invalidation path every view, ask and read guard follows', () => {
  const heard = [];
  const listener = (event) => heard.push(event.detail);
  libraryEvents.addEventListener('changed', listener);
  try {
    memoryChanged(null);
    memoryChanged('p1');
  } finally {
    libraryEvents.removeEventListener('changed', listener);
  }
  assert.deepEqual(heard, [null, 'p1']);
  const files = readdirSync(join(SRC, 'components')).filter((name) => /\.jsx?$/.test(name)).map((name) => join('components', name)).concat(['memory.js', 'live.js']);
  for (const name of files) {
    const source = readFileSync(join(SRC, name), 'utf8');
    assert.doesNotMatch(source, /memoryEvents/, name); // no second path to forget to follow
    if (!/libraryEvents\.addEventListener\('changed'/.test(source)) continue;
    for (const line of source.split('\n').filter((l) => /event\.detail\s*[!=]==/.test(l))) {
      assert.match(line, /null/, `${name}: a change naming no project is every project's: ${line.trim()}`);
    }
  }
});

// S1-21a follow-up MB1: a failed action in the memory views keeps its error and the researcher's unsaved text across
// the views' reread (the record is read again; what was not saved is said, and their own words stay with it).
test('a failed memory action says what was not saved and keeps the researcher\'s text', async () => {
  const { attempt } = await import('../src/memory.js');
  const { ApiError } = await import('../src/api.js');
  assert.equal(await attempt(t, async () => ({ ok: true })), null);
  const failed = await attempt(t, async () => { throw new ApiError(400, 'personal_from_strict', 'x'); }, 'I prefer tables');
  assert.equal(failed.draft, 'I prefer tables');
  assert.equal(failed.problem, t('errors.personal_from_strict'));
  for (const name of ['Memory.jsx', 'LeftOff.jsx']) {
    const source = readFileSync(join(SRC, 'components', name), 'utf8');
    assert.match(source, /<Unsaved /, name); // said outside the rows a reread replaces
    assert.match(source, /onFailed=/, name);
  }
  assert.doesNotMatch(readFileSync(join(SRC, 'components', 'Memory.jsx'), 'utf8'), /finally \{\s*onChanged/);
});

// S1-21a follow-up MB4: a turn started here makes the project's latest turn new, so Where we left off is read again
// (the backend then says not to show it) rather than staying on screen from before.
test('Where we left off is read again once a turn of this conversation starts', () => {
  // Q12 (PR 51 recheck 3): read again in place, never remounted (a remount dropped a failed action's notice)
  const source = readFileSync(join(SRC, 'components', 'ConversationView.jsx'), 'utf8');
  assert.match(source, /<LeftOff projectId=\{projectId\} turn=\{live\?\.runId \?\? null\} \/>/);
  assert.doesNotMatch(source, /<LeftOff key=/);
  assert.match(readFileSync(join(SRC, 'components', 'LeftOff.jsx'), 'utf8'), /\}, \[turn\]\);/);
});

// Q12 (PR 51 recheck 3): what an action could not save is kept outside the components that show it (by view and project),
// so a remount (a new conversation's first turn, the views opened again) never drops it, and another project's is never
// shown; nothing resets it but Dismiss, or a later failure in the same view.
test('a failed action\'s notice outlives a remount of the view that shows it', async () => {
  const { keepUnsaved, keptUnsaved } = await import('../src/memory.js');
  keepUnsaved('leftOff:p1', { problem: 'Not saved', draft: 'my words', id: 'r1' });
  assert.deepEqual(keptUnsaved('leftOff:p1'), { problem: 'Not saved', draft: 'my words', id: 'r1' });
  assert.equal(keptUnsaved('leftOff:p2'), null);
  assert.equal(keptUnsaved('settings:p1'), null);
  keepUnsaved('leftOff:p1', null);
  assert.equal(keptUnsaved('leftOff:p1'), null);
  for (const name of ['Memory.jsx', 'LeftOff.jsx']) {
    const source = readFileSync(join(SRC, 'components', name), 'utf8');
    assert.match(source, /useUnsaved\(/, name);
    assert.doesNotMatch(source, /setUnsaved\(null\); load\(\)/, name);
  }
});

// PR 51 fix batch 3, internal review: a kept notice goes once the same record's action succeeds, and a kept draft is
// let go (not only hidden) once a completed read no longer holds its record or any later version of it
test('a kept notice is cleared by a later success on the same record', async () => {
  const { settled } = await import('../src/memory.js');
  const kept = { problem: 'Not saved', draft: 'my words', id: 'r1' };
  assert.equal(settled(kept, null, 'r1'), null); // the same record's action succeeded
  assert.equal(settled(kept, null, 'r2'), kept); // another record's
  const failed = { problem: 'Again', draft: null, id: 'r2' };
  assert.equal(settled(kept, failed, 'r2'), failed);
});

// PR 51 fix batch 3, internal recheck: every action names its record, and an unnamed one clears nothing
test('a notice is cleared only by a success on the record it names', async () => {
  const { settled } = await import('../src/memory.js');
  const unnamed = { problem: 'Not saved', draft: null, id: null };
  assert.equal(settled(unnamed, null, null), unnamed);
  const source = readFileSync(join(SRC, 'components', 'Memory.jsx'), 'utf8');
  assert.match(source, /act\(\(\) => changeRecord\(record, \{ status: 'confirmed' \}\), null, record\.id\)/);
  assert.match(source, /act\(\(\) => forgetRecord\(record\), null, record\.id\)/);
});

// Q15 (PR 51 recheck 4; structural, for Harold): whether a kept draft's record still exists is the server's word
// (GET /api/memory/state: present, with its current version, or gone), never inferred from what a view lists. The text
// is shown beside the current version when the view lists it; kept but not shown while the record exists unlisted
// (memory off, moved, filtered) or while the view reads again; let go only once the server says gone. The earlier cases
// (one edit, Q13's two edits, a Forget, a paper's deletion, a reread) still hold.
test('a kept draft follows the server\'s word on its record, not what the view lists', async () => {
  const { attempt, draftShown, withState } = await import('../src/memory.js');
  const { ApiError } = await import('../src/api.js');
  const kept = await attempt(t, async () => { throw new ApiError(400, 'personal_from_strict'); }, 'my words', 'p1');
  const present = (current) => ({ state: 'present', current });
  // memory switched off: Things to confirm lists nothing, the proposal is still there: kept, not shown
  assert.equal(draftShown(kept, [], present('p1')), null);
  assert.equal(withState(kept, present('p1')), kept);
  assert.equal(draftShown(kept, [{ id: 'p1' }], present('p1')), 'my words'); // listed again: shown again
  // edited since, once or twice (r0, r1, r2): shown beside its current version
  assert.equal(draftShown(kept, [{ id: 'r2' }], present('r2')), 'my words');
  assert.equal(draftShown(kept, [{ id: 'r2' }], null), null); // not yet answered, or reading again
  assert.equal(draftShown(kept, null, present('p1')), null);
  // forgotten, or gone with its paper: let go
  assert.deepEqual(withState(kept, { state: 'gone' }), { ...kept, draft: null });
  assert.equal(withState(kept, null), kept);
  const unsaved = readFileSync(join(SRC, 'components', 'Memory.jsx'), 'utf8');
  assert.match(unsaved, /memoryState\(\[id\]\)/);
  for (const name of ['Memory.jsx', 'LeftOff.jsx']) {
    assert.match(readFileSync(join(SRC, 'components', name), 'utf8'), /<Unsaved [^>]*read=\{found\}/, name);
  }
  assert.match(readFileSync(join(SRC, 'components', 'LeftOff.jsx'), 'utf8'), /return unsaved \? /); // said unshown too
  const memory = readFileSync(join(SRC, 'memory.js'), 'utf8');
  assert.doesNotMatch(memory, /draftFor|purged|replaced/); // no inference from listings is left
});
