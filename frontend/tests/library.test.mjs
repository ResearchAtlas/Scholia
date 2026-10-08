// The Library's and background runs' helpers (src/library.js, src/runs.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { changes, detailsOf, reasonKey, rectStyle, sortFiles, supported, unsettled, validYear, byPage, authorNames,
  typeKey } from '../src/library.js';
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
    'file_missing', 'interrupted', 'not_found', 'something new'];
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
    'lookup_locked', 'declined', 'project_changed', 'closing', 'disk_full', 'write_failed', 'passphrase_required']) {
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
