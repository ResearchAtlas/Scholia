// The Library's and background runs' helpers (src/library.js, src/runs.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { changes, detailsOf, reasonKey, rectStyle, sortFiles, supported, unsettled, validYear, byPage, authorNames,
  typeKey, viewOf, pointing, hovering, isPointed, NOT_POINTED, unionRect, refreshed, takeSaved, newest, requestsOf, REQUEST_FILE_BYTES, MAX_FILE_BYTES, LOOKUP_OUTCOMES, addFiles, readAsks, followAsks, asksChanged, afterRead, pollsAsks, NO_ASKS, cancelledKey, heldPages, withNear, MAX_HELD_PAGES,
  detailsSource, latestLookup } from '../src/library.js';
import { makeT } from '../src/i18n/index.js';
import { followRun, fraction, runOutcome } from '../src/runs.js';
import { deletePath } from '../src/backups.js';
import { getBlob } from '../src/api.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zh from '../src/i18n/zh-CN.json' with { type: 'json' };

test('only the formats Scholia reads are sent; the rest are named', () => {
  for (const name of ['a.pdf', 'B.PDF', 'c.docx', 'd.html', 'e.htm', 'f.md', 'g.markdown', 'h.tex']) assert.ok(supported(name), name);
  for (const name of ['a.txt', 'b.doc', 'pdf', '.pdf', 'c.pdf.exe']) assert.ok(!supported(name), name);
  const { kept, skipped } = sortFiles([{ name: 'a.pdf' }, { name: 'notes.txt' }, { name: 'b.tex' }]);
  assert.deepEqual(kept.map((f) => f.name), ['a.pdf', 'b.tex']);
  assert.deepEqual(skipped, ['notes.txt']);
});

test('every state, reason and type the backend gives has its text in both catalogs', () => {
  const reasons = ['ocr_waiting', 'no_text', 'not_read', 'stopped', 'time_limit', 'unreadable_file', 'encrypted_file',
    'file_missing', 'interrupted', 'not_found', 'outdated', 'something new'];
  const keys = [...['reading', 'ready', 'needs_attention'].map((s) => `library.state.${s}`), ...reasons.map(reasonKey),
    ...['application/pdf', 'text/html', 'text/markdown', 'application/x-tex', 'x/unknown',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document'].map(typeKey)];
  for (const key of keys) {
    assert.ok(key in en, key);
    assert.ok(key in zh, key);
  }
  assert.equal(reasonKey('something new'), 'library.reason.other');
  assert.equal(reasonKey(null), null);
});

test('the Library looks again only while something is under way', () => {
  assert.equal(unsettled(null), false);
  assert.equal(unsettled({ materials: [{ state: 'ready' }], asks: [] }), false);
  assert.equal(unsettled({ materials: [{ state: 'reading' }], asks: [] }), true);
  assert.equal(unsettled({ materials: [{ state: 'ready', lookup: { status: 'running' } }], asks: [] }), true);
  assert.equal(unsettled({ materials: [], asks: [{}] }), true);
});

test('the details form sends only what changed, authors one per line', () => {
  const material = { title: 'A Title', csl: { author: [{ family: 'Example', given: 'Ana' }, { literal: 'Bo' }],
    issued: { 'date-parts': [[2021]] }, 'container-title': 'Review', DOI: '10.5555/x' } };
  const before = detailsOf(material);
  assert.deepEqual(before, { title: 'A Title', authors: 'Example, Ana\nBo', year: '2021', venue: 'Review', doi: '10.5555/x' });
  assert.deepEqual(changes(before, before), {});
  assert.deepEqual(changes(before, { ...before, authors: 'Example, Ana\n\n  Cy  \n', year: '' }),
    { authors: ['Example, Ana', 'Cy'], year: null });
  assert.deepEqual(changes(before, { ...before, year: '1999', doi: ' 10.5555/y ' }), { year: 1999, doi: '10.5555/y' });
  assert.ok(validYear('') && validYear('2024') && !validYear('24') && !validYear('3000') && !validYear('20x4'));
  assert.deepEqual(authorNames(material.csl), ['Ana Example', 'Bo']);
});

test('a passage box sits over its page by fractions of the page', () => {
  assert.deepEqual(rectStyle([0.1, 0.2, 0.55, 0.25]), { left: '10%', top: '20%', width: '45%', height: '5%' });
  assert.deepEqual(rectStyle([0.5, 0.5, 0.4, 0.4]).width, '0%'); // never negative
  const pages = byPage([{ id: 'a', page: 1 }, { id: 'b', page: 2 }, { id: 'c', page: 1 }, { id: 'd', page: null }]);
  assert.deepEqual([...pages.entries()].map(([n, ps]) => [n, ps.map((p) => p.id)]), [[1, ['a', 'c']], [2, ['b']]]);
});

test('a material is deleted through its own endpoint, with the dialog choices', () => {
  assert.equal(deletePath('material', 'm 1', { everywhere: true }), '/api/materials/m%201?purge_backups=true');
  assert.equal(deletePath('project', 'p'), '/api/projects/p');
});

test('a run is followed until it ends, and its outcome named', async () => {
  const rows = [{ status: 'running', progress: { done: 1, total: 4 } }, { status: 'succeeded', result: { file: '/x.zip' } }];
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ runs: [rows.shift()] }) });
  try {
    const row = await followRun('r1', (current) => seen.push(current.progress), async () => {});
    assert.equal(row.status, 'succeeded');
    assert.deepEqual(seen, [{ done: 1, total: 4 }]);
  } finally {
    globalThis.fetch = original;
  }
  assert.deepEqual(runOutcome({ status: 'succeeded' }), { ok: true });
  assert.deepEqual(runOutcome({ status: 'failed', result: { reason: 'disk_full' } }), { ok: false, code: 'disk_full' });
  assert.deepEqual(runOutcome({ status: 'cancelled', cancel_reason: 'researcher' }), { ok: false, key: 'runs.stopped' });
  assert.deepEqual(runOutcome({ status: 'cancelled', cancel_reason: 'revoked' }), { ok: false, key: 'runs.revoked' });
  assert.deepEqual(runOutcome({ status: 'interrupted' }), { ok: false, key: 'runs.interrupted' });
  assert.equal(fraction({ done: 3, total: 4 }), 0.75);
  assert.equal(fraction({ done: 0, total: 0 }), null);
  assert.equal(fraction(null), null);
});

test('the reasons a run ends with have their texts in both catalogs', () => {
  for (const code of ['unsupported_file', 'file_too_large', 'not_retryable', 'not_a_pdf', 'file_missing', 'unreadable_file',
    'encrypted_file', 'time_limit', 'title_needed', 'invalid_doi', 'ask_closed', 'ask_invalid', 'invalid_answer',
    'lookup_locked', 'declined', 'project_changed', 'closing', 'disk_full', 'write_failed', 'passphrase_required', 'unavailable',
    'refused']) {
    assert.ok(`errors.${code}` in en && `errors.${code}` in zh, code);
  }
});

test('a page image is fetched past the browser cache, so a deleted paper\'s page never comes back from it', async () => {
  const original = globalThis.fetch;
  const asked = [];
  globalThis.fetch = async (path, options) => { asked.push(options); return { ok: true, blob: async () => 'png' }; };
  try {
    assert.equal(await getBlob('/api/material-versions/v/pages/1'), 'png');
    assert.equal(asked[0].cache, 'no-store');
  } finally {
    globalThis.fetch = original;
  }
});

test('a paper shows its pages only while its version is a PDF', () => {
  const pdf = { version: { id: 'v1', media_type: 'application/pdf' } };
  const markdown = { version: { id: 'v2', media_type: 'text/markdown' } }; // its replacement, read already elsewhere
  assert.equal(viewOf(pdf, 'pages'), 'pages');
  assert.equal(viewOf(pdf, 'text'), 'text');
  assert.equal(viewOf(markdown, 'pages'), 'text'); // the PDF's choice no longer applies: its passages show
  assert.equal(viewOf({ version: null }, 'pages'), 'text');
});

// The passages' highlight as the page keeps it: a state set as React's setter sets it.
function passages() {
  let state = NOT_POINTED;
  const set = (update) => { state = typeof update === 'function' ? update(state) : update; };
  return { props: (id) => pointing(id, set), lit: (ids) => ids.filter((id) => isPointed(state, id)) };
}

test('a passage is reached by Tab and highlighted on focus as on hover, once on its page however many lines', () => {
  const { props, lit } = passages();
  assert.equal(props('p1').tabIndex, 0);
  props('p1').onFocus();
  assert.deepEqual(lit(['p1']), ['p1']);
  props('p1').onBlur();
  props('p1').onMouseEnter();
  assert.deepEqual(lit(['p1']), ['p1']);
  props('p1').onMouseLeave();
  assert.deepEqual(lit(['p1']), []);
  assert.deepEqual(Object.keys(hovering('p1', () => {})), ['onMouseEnter', 'onMouseLeave']); // a page's line boxes
  assert.deepEqual(unionRect([[0.1, 0.2, 0.8, 0.22], [0.12, 0.23, 0.5, 0.25]]), [0.1, 0.2, 0.8, 0.25]);
});

test('the pointer never takes the highlight from the passage with focus, nor focus from the one pointed at', () => {
  const { props, lit } = passages();
  props('A').onFocus(); // Tab reaches A, and the list scrolls B under the resting pointer
  props('B').onMouseEnter();
  assert.deepEqual(lit(['A', 'B']), ['A', 'B']); // B's own highlight, beside A's
  props('B').onMouseLeave();
  assert.deepEqual(lit(['A', 'B']), ['A']); // A keeps its highlight while it keeps focus
  props('B').onMouseEnter();
  props('A').onBlur(); // focus leaves A: the pointer is still on B
  assert.deepEqual(lit(['A', 'B']), ['B']);
  props('C').onMouseEnter(); // the pointer moves from B to C, and B's leave arrives late
  props('B').onMouseLeave();
  assert.deepEqual(lit(['A', 'B', 'C']), ['C']);
});

test('details a lookup saves while one field is edited fill the form, and a save sends only that field', () => {
  const shown = detailsOf({ title: 'paper', csl: {} }); // the form opened before the lookup
  const form = { ...shown, venue: 'My Own Venue' }; // the researcher types a venue
  const saved = detailsOf({ title: 'A Resolved Title', csl: { author: [{ literal: 'Ana Example' }],
    issued: { 'date-parts': [[2024]] }, 'container-title': 'Journal of Synthetic Studies', DOI: '10.5555/x' } });
  const merged = refreshed(shown, form, saved);
  assert.deepEqual(merged, { ...saved, venue: 'My Own Venue' });
  assert.deepEqual(changes(saved, merged), { venue: 'My Own Venue' }); // not the stale blanks of the other fields
  assert.deepEqual(refreshed(shown, shown, saved), saved); // nothing edited: all of what was saved
});

test('the form takes newly saved details against the ones it last took, however late React runs the update', () => {
  const blank = detailsOf({ title: 'paper', csl: {} });
  const looked = detailsOf({ title: 'A Resolved Title', csl: { 'container-title': 'Journal of Synthetic Studies',
    DOI: '10.5555/x' } });
  const corrected = { ...looked, venue: 'Journal of Synthetic Studies, Second Series' }; // a later save elsewhere
  const shown = { current: blank }; // the ref of the saved details the form last took
  const queued = []; // React queues the updates, to run them at its next render
  const later = (update) => queued.push(update);
  takeSaved(shown, looked, later); // a lookup saved its details
  takeSaved(shown, corrected, later); // and a save came before React ran the first update
  assert.deepEqual(shown.current, corrected); // the ref moved on before either update ran
  const form = queued.reduce((current, update) => update(current), { ...blank, title: 'My Own Title' });
  assert.deepEqual(form, { ...corrected, title: 'My Own Title' }); // the blanks were not edits: they take what was saved
  assert.deepEqual(changes(corrected, form), { title: 'My Own Title' }); // a save sends only the field typed
});

test('a slow read of the first project that answers after the switch to another is not shown', async () => {
  const asked = newest(); // as the Library's reads are made: each newest, or dropped
  let shown = null;
  const load = async (answer) => { const current = asked(); const found = await answer; if (current()) shown = found; };
  let answerFirst;
  const first = load(new Promise((resolve) => { answerFirst = resolve; })); // the first project's read, slow
  await load(Promise.resolve('the second project\'s papers')); // the switch: the second project's read
  answerFirst('the first project\'s papers');
  await first;
  assert.equal(shown, 'the second project\'s papers');
});

test('a selection is sent in as few requests as fit the backend\'s body limit, each under it', () => {
  const MiB = 1024 * 1024;
  const file = (name, size) => ({ name, size });
  const small = [file('a.pdf', 2 * MiB), file('b.md', 1000), file('c.docx', 5 * MiB)];
  assert.deepEqual(requestsOf(small).map((g) => g.map((f) => f.name)), [['a.pdf', 'b.md', 'c.docx']]); // one batch
  const large = [file('a.pdf', 60 * MiB), file('b.pdf', 60 * MiB), file('c.md', 1000), file('d.pdf', MAX_FILE_BYTES)];
  const groups = requestsOf(large);
  assert.deepEqual(groups.map((g) => g.map((f) => f.name)), [['a.pdf'], ['b.pdf', 'c.md'], ['d.pdf']]);
  for (const group of groups) assert.ok(group.reduce((sum, f) => sum + Math.ceil(f.size / 3) * 4, 0) <= REQUEST_FILE_BYTES);
  // backend/local_guard.py MAX_BODY: one largest file in base64 and 64 KiB for the JSON around it
  assert.equal(REQUEST_FILE_BYTES + 64 * 1024, (100 * MiB + 2 - ((100 * MiB + 2) % 3)) / 3 * 4 + 64 * 1024);
  assert.equal(requestsOf(Array.from({ length: 25 }, (_, i) => file(`${i}.md`, 10))).map((g) => g.length).join(), '20,5');
});

test('a request over the body limit has its text in both catalogs', () => {
  for (const key of ['errors.request_too_large', 'errors.length_required']) assert.ok(key in en && key in zh, key);
});

test('every lookup outcome a paper\'s details name has its text in both catalogs, a file not read yet included', () => {
  assert.ok(LOOKUP_OUTCOMES.includes('not_read'));
  for (const key of [...LOOKUP_OUTCOMES.map((outcome) => `library.source.${outcome}`), 'errors.not_read', 'library.readAgain']) {
    assert.ok(key in en && key in zh, key);
  }
});

test('a paper\'s details name the record they came from, whatever its latest lookup did, which shows on its own line', () => {
  const t = makeT('en');
  const date = (value) => value.slice(0, 10);
  const resolved = { checked_by: 'lookup', source_key: 'doi:10.5555/x', resolved_at: '2026-01-02T00:00:00.000Z',
    checked_at: '2026-01-02T00:00:00.000Z' };
  // Resolved through Crossref, then the file replaced and its new lookup found no record: the details are Crossref's still.
  const failed = { ...resolved, lookup: { status: 'succeeded', outcome: 'not_found', source: null } };
  assert.equal(detailsSource(t, failed, {}, date), 'From Crossref, 2026-01-02');
  assert.equal(latestLookup(t, failed), en['library.lookup.not_found']);
  for (const [key, service] of [['openalex:W1', 'OpenAlex'], ['arxiv:2401.00001', 'arXiv']]) {
    const found = { ...resolved, source_key: key, lookup: { status: 'succeeded', outcome: 'resolved', source: null } };
    assert.equal(detailsSource(t, found, {}, date), `From ${service}, 2026-01-02`);
    assert.equal(latestLookup(t, found), null); // the lookup that gave them says nothing more
  }
  const running = { ...resolved, lookup: { status: 'running' } };
  assert.equal(latestLookup(t, running), en['library.source.lookingUp']);
  const edited = { checked_by: 'researcher', checked_at: '2026-03-04T00:00:00.000Z', lookup: { status: 'succeeded', outcome: 'unavailable' } };
  assert.equal(detailsSource(t, edited, {}, date), 'Edited by you, 2026-03-04');
  assert.equal(latestLookup(t, edited), en['library.lookup.unavailable']);
  const file = { checked_by: null, lookup: { status: 'succeeded', outcome: 'not_found' } };
  assert.equal(detailsSource(t, file, {}, date), en['library.source.not_found']); // from the file: said there once
  assert.equal(latestLookup(t, file), null);
  for (const key of [...LOOKUP_OUTCOMES.map((outcome) => `library.lookup.${outcome}`), 'library.fact.lookup']) {
    assert.ok(key in en && key in zh, key);
  }
});

test('a drop sent in several requests is one batch: the first opens it, the next add to it, the last closes it', async () => {
  const MiB = 1024 * 1024;
  const realFetch = globalThis.fetch;
  const realReader = globalThis.FileReader;
  globalThis.FileReader = class { // as the browser reads a file: a data URL of its bytes
    readAsDataURL(blob) {
      blob.arrayBuffer().then((bytes) => { this.result = `data:;base64,${Buffer.from(bytes).toString('base64')}`; this.onload(); });
    }
  };
  const sent = [];
  globalThis.fetch = async (path, init) => { // the backend: the first request's lookup run is the batch
    const body = JSON.parse(init.body);
    sent.push({ size: init.body.length, body: { ...body, files: body.files.map((f) => f.name) } });
    const n = sent.length;
    const materials = body.files.map((f, i) => ({ id: `m${n}${i}`, existing: false, version_id: `v${n}${i}`, run_id: `r${n}${i}` }));
    return new Response(JSON.stringify({ materials, lookup_run_id: body.batch ?? 'lookup1' }), { status: 201 });
  };
  try {
    const part = new Uint8Array(MiB);
    const file = (name) => new File(Array.from({ length: 60 }, () => part), name); // 60 MiB each
    const added = await addFiles('p1', [file('a.pdf'), file('b.pdf')]);
    assert.deepEqual(sent.map((r) => r.body), [
      { files: ['a.pdf'], more: true }, // opens the drop's batch: its lookup waits for the rest
      { files: ['b.pdf'], batch: 'lookup1' }]); // adds to it and closes it: one question in a Local only project
    assert.ok(sent.every((r) => r.size <= REQUEST_FILE_BYTES + 64 * 1024)); // each under the backend's limit
    assert.deepEqual(added.materials.map((m) => m.id), ['m10', 'm20']);
    assert.equal(added.lookup_run_id, 'lookup1');

    // A drop of 45 files: three requests, one batch.
    sent.length = 0;
    const many = await addFiles('p1', Array.from({ length: 45 }, (_, i) => new File([`# Paper ${i}`], `${i}.md`)));
    assert.deepEqual(sent.map((r) => [r.body.files.length, r.body.batch, r.body.more]),
      [[20, undefined, true], [20, 'lookup1', true], [5, 'lookup1', undefined]]);
    assert.equal(many.materials.length, 45);
  } finally {
    globalThis.fetch = realFetch;
    globalThis.FileReader = realReader;
  }
});

test('a conversation\'s questions read before a switch to another conversation never show in the new one', async () => {
  const realFetch = globalThis.fetch;
  const answers = {};
  globalThis.fetch = (path) => new Promise((resolve) => { answers[new URL(path, 'http://x').searchParams.get('conversation_id')] =
    (asks) => resolve(new Response(JSON.stringify({ asks, working: asks.length }), { status: 200 })); });
  try {
    const asked = newest(); // the conversation's reads, as useConversationAsks makes them
    const shown = [];
    const first = readAsks(asked, 'c1', (found) => shown.push(found)); // the first conversation's read, slow
    const second = readAsks(asked, 'c2', (found) => shown.push(found)); // after the switch
    await new Promise((r) => setTimeout(r, 0));
    answers.c2([{ ask_id: 'a2' }]);
    await second;
    answers.c1([{ ask_id: 'a1' }]); // the first conversation's question answers late
    await first;
    assert.deepEqual(shown.map((found) => found?.asks.map((a) => a.ask_id)), [['a2']]); // only the second's
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('a draft conversation\'s questions are read from its first id on, and stay when the window learns the same id', async () => {
  const realFetch = globalThis.fetch;
  const asked_for = [];
  globalThis.fetch = async (path) => {
    const id = new URL(path, 'http://x').searchParams.get('conversation_id');
    asked_for.push(id);
    return new Response(JSON.stringify({ asks: [{ ask_id: `ask-of-${id}` }], working: 1 }), { status: 200 });
  };
  try {
    const asked = newest();
    const shown = [];
    await readAsks(asked, null, (found) => shown.push(found)); // no conversation yet: nothing is read or shown
    await readAsks(asked, 'draft-1', (found) => shown.push(found)); // its first message made it: its files' question
    await readAsks(asked, 'draft-1', (found) => shown.push(found)); // the window learns of it: the same id
    assert.deepEqual(asked_for, ['draft-1', 'draft-1']);
    assert.deepEqual(shown.map((found) => found.asks[0].ask_id), ['ask-of-draft-1', 'ask-of-draft-1']);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('an upload that ends after its conversation\'s view was rebuilt wakes the view now showing it', async () => {
  const realFetch = globalThis.fetch;
  let lookupRuns = false; // the upload's lookup, once it has committed
  globalThis.fetch = async () => new Response(JSON.stringify(lookupRuns
    ? { asks: [{ ask_id: 'the-lookups' }], working: 1 } : { asks: [], working: 0 }), { status: 200 });
  try {
    const view = () => { // one instance of the conversation's questions, as useConversationAsks keeps them
      const asked = newest();
      const state = { asks: [], reads: 0 };
      const load = () => readAsks(asked, 'c1', (found) => { if (found) state.asks = found.asks; state.reads += 1; });
      return { state, load };
    };
    const first = view(); // the draft's view, where the slow upload starts
    const leave = followAsks('c1', first.load);
    leave(); // the resend made it the parent's: Shell rebuilds the view for the same conversation
    const second = view();
    const stop = followAsks('c1', second.load);
    await second.load(); // its first read: nothing yet, and nothing at work, so it would not look again
    assert.deepEqual(second.state.asks, []);
    lookupRuns = true; // the upload commits, with its lookup
    asksChanged('c1'); // and says so
    asksChanged('another'); // a change in another conversation wakes nothing here
    await new Promise((r) => setTimeout(r, 0));
    assert.deepEqual(second.state.asks.map((a) => a.ask_id), ['the-lookups']); // the view shown reads it
    assert.equal(first.state.reads, 0); // the one rebuilt away, never
    assert.equal(second.state.reads, 2);
    stop();
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('a run stopped for a reason it recorded says that reason, and one stopped with none says it was stopped', () => {
  const declined = runOutcome({ status: 'cancelled', cancel_reason: 'researcher', result: { reason: 'declined' } });
  const limited = runOutcome({ status: 'cancelled', cancel_reason: 'limit', result: { reason: 'time_limit' } });
  assert.deepEqual([declined, limited], [{ ok: false, code: 'declined' }, { ok: false, code: 'time_limit' }]);
  for (const { code } of [declined, limited]) assert.ok(`errors.${code}` in en && `errors.${code}` in zh, code);
  assert.deepEqual(runOutcome({ status: 'cancelled', cancel_reason: 'researcher', result: null }), { ok: false, key: 'runs.stopped' });
  assert.deepEqual(runOutcome({ status: 'cancelled', cancel_reason: 'revoked', result: { reason: 'project_changed' } }),
    { ok: false, key: 'runs.revoked' }); // a project's change says so, as before
});

test('a read of a conversation\'s questions that fails keeps the view looking until a read succeeds', async () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    if (calls === 1) throw new TypeError('the connection dropped'); // the wake-up's read fails
    return new Response(JSON.stringify({ asks: [{ ask_id: 'the-lookups' }], working: 1 }), { status: 200 });
  };
  try {
    const asked = newest();
    let state = NO_ASKS; // as useConversationAsks keeps it
    const load = () => readAsks(asked, 'c1', (found) => { state = afterRead(state, found); });
    await load(); // woken by the upload: the read fails
    assert.deepEqual(state.asks, []);
    assert.equal(pollsAsks(state, null), true); // not known whether work remains: it looks again
    await load(); // the next turn
    assert.deepEqual(state.asks.map((a) => a.ask_id), ['the-lookups']);
    assert.equal(pollsAsks(afterRead(state, { asks: [], working: 0 }), null), false); // a read saying none remains ends it
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('a lookup stopped says why by what it recorded: skipped on its question, stopped, or its project changed', () => {
  assert.equal(cancelledKey({ status: 'cancelled', cancel_reason: 'researcher', reason: 'declined' }), 'library.source.declined');
  assert.equal(cancelledKey({ status: 'cancelled', cancel_reason: 'researcher', reason: null }), 'runs.stopped'); // Cancel
  assert.equal(cancelledKey({ status: 'cancelled', cancel_reason: 'revoked', reason: 'project_changed' }),
    'library.source.projectChanged');
  for (const key of ['library.source.declined', 'runs.stopped', 'library.source.projectChanged']) assert.ok(key in en && key in zh);
  // The background-run list follows the same rule.
  assert.deepEqual(runOutcome({ status: 'cancelled', cancel_reason: 'researcher', result: null }), { ok: false, key: 'runs.stopped' });
});

test('a long PDF holds the rendered images of the pages near the view only, a bounded number of them', () => {
  let near = new Set();
  for (let page = 1; page <= 3; page += 1) near = withNear(near, page, true); // the first screens
  assert.deepEqual([...heldPages(near)], [1, 2, 3]);
  const same = withNear(near, 2, true);
  assert.equal(same, near); // a page reported near again changes nothing
  for (let page = 1; page <= 30; page += 1) near = withNear(near, page, page >= 18 && page <= 30); // scrolled down
  assert.deepEqual([...heldPages(near)].sort((a, b) => a - b), [20, 21, 22, 23, 24, 25, 26, 27]); // the middle of it
  assert.equal(heldPages(near).size, MAX_HELD_PAGES);
  assert.ok(!heldPages(near).has(1)); // a page far behind lets its image go
});
