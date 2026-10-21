// Answers with citations (S1-19b; slice-1 spec F4, sections 3 and 7.5; ticket 71): GitHub Markdown in
// answers with skipHtml, safeHref and imageAsLink still applied; citation chips, in a table cell too,
// with the quote and page on hover or focus, opening the paper at its passage; abstract-only and
// retracted chips marked; an unsupported sentence underlined, its text unchanged; a citation that does
// not exist never shown as a chip; Details' trace summed up from its events; the papers chosen for a
// message; and every new string in both catalogs.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import Answer, { Chip } from '../src/markdown.js';
import { readTrace, summarize } from '../src/trace.js';
import { MAX_CHOSEN, choosePapers, chosenPapers, openPaperAt, takeOpening, watchChosen } from '../src/library.js';
import { NOTICES } from '../src/live.js';
import en from '../src/i18n/en.json' with { type: 'json' };
import zhCN from '../src/i18n/zh-CN.json' with { type: 'json' };

const t = (key, values) => `${key}${values ? JSON.stringify(values) : ''}`;
const render = (text, citations, onOpen) => renderToStaticMarkup(h(Answer, { text, citations, t, onOpen }));
const cite = (text, marker, extra = {}, from = 0) => ({ marker, offset: text.indexOf(marker, from), existence: 'ok',
  support: 'supported', title: 'Wage Floors', page: 4, quote: 'Minimum wages raise earnings.', evidence_type: 'full_text',
  retracted: false, material_id: 'm1', passage_id: 'p1', ordinal: 3, ...extra });

test('GitHub Markdown renders tables, strikethrough and autolinks, and unsafe links and images stay inert', () => {
  const html = render('| A | B |\n|---|---|\n| 1 | 2 |\n\n~~old~~ www.example.com <javascript:alert(1)> [x](javascript:alert(2))\n\n'
    + '![chart](https://images.example.com/c.png) <b>raw</b>', []);
  assert.match(html, /<table>.*<td>1<\/td>/s);
  assert.match(html, /<del>old<\/del>/);
  assert.match(html, /<a href="http:\/\/www\.example\.com" target="_blank" rel="noreferrer noopener">/);
  assert.doesNotMatch(html, /href="javascript:/); // never a link
  assert.match(html, /<span>javascript:alert\(1\)<\/span>/);
  assert.doesNotMatch(html, /<img/); // a remote image is a link, never loaded
  assert.match(html, /<a href="https:\/\/images\.example\.com\/c\.png"[^>]*>turn\.image\{&quot;label&quot;:&quot;chart&quot;\}/);
  assert.doesNotMatch(html, /<b>/); // no raw HTML
});

test('a citation is a chip with its quote and page, in a table cell too, and a marker in code stays code', () => {
  const text = '| Effect | Source |\n|---|---|\n| Earnings rise | [S1.1] |\n\nWritten `[S1.1]` in code.';
  const html = render(text, [cite(text, '[S1.1]'), cite(text, '[S1.1]', {}, text.indexOf('`'))]);
  const cell = /<td><span class="cite group">(.*?)<\/span><\/span><\/td>/s.exec(html);
  assert.ok(cell, 'the chip is in its table cell');
  assert.match(cell[1], /<button type="button" class="cite-chip "[^>]*aria-label="cite.open\{&quot;number&quot;:&quot;1&quot;/);
  assert.match(cell[1], /role="tooltip" class="cite-card"><span class="cite-title">Wage Floors · cite\.page\{&quot;page&quot;:4\}/);
  assert.match(cell[1], /“Minimum wages raise earnings\.”/);
  const described = /aria-describedby="([^"]+)"/.exec(cell[1])[1];
  assert.match(cell[1], new RegExp(`id="${described}" role="tooltip"`)); // the card describes the chip, for the keyboard too
  assert.match(html, /<code>\[S1\.1\]<\/code>/);
});

test('pressing a chip opens its paper at its passage', () => {
  const citation = cite('x [S1.1]', '[S1.1]');
  const opened = [];
  // Chip is a function component using useId: drawn once through the server renderer, then its button's handler taken.
  let button = null;
  const Probe = () => {
    const element = Chip({ citation, t, onOpen: (c) => opened.push(c) });
    button = element.props.children[0];
    return element;
  };
  renderToStaticMarkup(h(Probe));
  assert.equal(button.type, 'button');
  button.props.onClick();
  assert.deepEqual(opened, [citation]);
});

test('abstract-only, record-only and retracted chips are marked, and an unsupported sentence is underlined as written', () => {
  const text = 'Earnings rise a lot [S1.1]. Prices hold [S2.1][S3.1]. Rents rise [S4.1].';
  const html = render(text, [cite(text, '[S1.1]', { support: 'unsupported' }), cite(text, '[S2.1]', { evidence_type: 'abstract' }),
    cite(text, '[S3.1]', { evidence_type: 'metadata', retracted: true }), cite(text, '[S4.1]', { support: 'partly' })]);
  assert.match(html, /<span class="unsupported">Earnings rise a lot <\/span><span class="cite group"><button type="button" class="cite-chip cite-unsupported"/);
  assert.match(html, /cite\.unsupported/);
  assert.match(html, /class="cite-chip cite-partial"[^>]*>2<\/button>.*cite\.abstract/s);
  assert.match(html, /class="cite-chip cite-retracted cite-partial"[^>]*>3!<\/button>.*cite\.retracted · cite\.metadata/s);
  assert.equal((html.match(/class="unsupported"/g) ?? []).length, 1); // a partly supported sentence is not underlined
  assert.equal(html.replace(/<[^>]+>/g, '').includes('Earnings rise a lot'), true); // the text unchanged
});

test('a marker after the sentence\'s end cites the sentence before it, as the backend reads it', () => {
  const text = 'Earnings rise. [S1.1] Prices fall. 收入上升。[S1.2]';
  const html = render(text, [cite(text, '[S1.1]', { support: 'unsupported' }), cite(text, '[S1.2]', { support: 'unsupported' })]);
  assert.match(html, /<span class="unsupported">Earnings rise\. <\/span><span class="cite group">/);
  assert.match(html, /<span class="unsupported">收入上升。<\/span><span class="cite group">/);
  assert.equal(html.replace(/<[^>]+>/g, '').includes('Prices fall.'), true);
});

test('an unsupported sentence is underlined across bold text, a link and a table row\'s cells', () => {
  const bold = '**Earnings rise.** [S1.1] Prices hold.';
  let html = render(bold, [cite(bold, '[S1.1]', { support: 'unsupported' })]);
  assert.match(html, /<strong><span class="unsupported">Earnings rise\.<\/span><\/strong>/);
  assert.doesNotMatch(html, /<span class="unsupported">[^<]*Prices/);
  const table = '| Effect | Source |\n|---|---|\n| Earnings rise. | [S1.1] |';
  html = render(table, [cite(table, '[S1.1]', { support: 'unsupported' })]);
  assert.match(html, /<td><span class="unsupported">Earnings rise\.<\/span><\/td>/);
  assert.doesNotMatch(html, /<th><span class="unsupported">/); // never past its row
  const mixed = 'First claim. Rents *rise* in [the panel](https://example.com/x) a lot [S1.1].';
  html = render(mixed, [cite(mixed, '[S1.1]', { support: 'unsupported' })]);
  assert.equal((html.match(/class="unsupported"/g) ?? []).length, 5); // "Rents ", "rise", " in ", "the panel", " a lot "
  assert.doesNotMatch(html, /<span class="unsupported">First claim/);
});

test('the underline stops at the sentence before, whatever node or line it ended in', () => {
  const under = (text) => [...render(text, [cite(text, '[S1.1]', { support: 'unsupported' })])
    .matchAll(/<span class="unsupported">([^<]*)<\/span>/g)].map((m) => m[1]);
  assert.deepEqual(under('**Bold.** Wages rose. [S1.1]'), ['Wages rose. ']);
  assert.deepEqual(under('*Old claim.* Wages rose [S1.1].'), ['Wages rose ']);
  assert.deepEqual(under('[Old claim.](https://e.com/x) Wages rose. [S1.1]'), ['Wages rose. ']);
  assert.deepEqual(under('工资上涨。**物价**下跌。[S1.1]'), ['物价', '下跌。']);
  assert.deepEqual(under('Prices fall\nEarnings rise [S1.1]'), ['Earnings rise ']);
  assert.deepEqual(under('Prices fall  \nEarnings rise [S1.1]'), ['Earnings rise ']);
  assert.deepEqual(under('**Earnings rise.** [S1.1]'), ['Earnings rise.']);
});

test('a citation that does not exist is never a chip, and an answer without its checks keeps its markers as text', () => {
  const text = 'Wages rise [S1.1]. Prices fall [S9.9]. Unclear [S1].';
  const html = render(text, [cite(text, '[S1.1]', { existence: 'source_removed' }), { ...cite(text, '[S9.9]'), existence: 'not_found' },
    { ...cite(text, '[S1]'), existence: 'not_found' }]);
  assert.doesNotMatch(html, /<button/);
  assert.match(html, /class="cite-missing" title="cite\.removed"/);
  assert.equal((html.match(/class="cite-missing" title="cite\.notFound"/g) ?? []).length, 2);
  const live = render(text, null);
  assert.doesNotMatch(live, /cite/);
  assert.match(live, /Wages rise \[S1\.1\]\. Prices fall \[S9\.9\]\./);
});

test('Details sums up the trace from its events, read a page at a time', async () => {
  const pages = {
    '-1': { events: [
      { seq: 0, type: 'route', data: { route: 'openrouter:m', retrieval: { route: 'literature', decided_by: 'fallback' } }, body_ref: null },
      { seq: 1, type: 'retrieval_trace', data: { mode: 'hybrid', selected: ['a', 'b'], rerank: { fallback: 'deadline' } }, body_ref: null },
    ], next: 1 },
    1: { events: [
      { seq: 2, type: 'step_started', data: { step: 0, phase: 'answer', route: 'openrouter:m', estimate_usd: 0.05 }, body_ref: null },
      { seq: 3, type: 'model_attempt', data: { step: 0, outcome: 'ok', cost_usd: 0.002, charge: 'reported' }, body_ref: 'aa' },
      { seq: 3.5, type: 'step_finished', data: { step: 0, cut: 'output_limit' }, body_ref: null }, // saved, cut
      { seq: 4, type: 'step_started', data: { step: 1, phase: 'judge', estimate_usd: 0.01 }, body_ref: null },
      { seq: 5, type: 'model_attempt', data: { step: 1, outcome: 'timeout' }, body_ref: 'bb' },
      { seq: 5.5, type: 'model_attempt', data: { step: 1, outcome: 'ok', body: 'cc' }, body_ref: null, body_removed: true },
      { seq: 5.6, type: 'model_attempt', data: { step: 1, outcome: 'ok', body_unavailable: true }, body_ref: null },
      { seq: 6, type: 'citation_checks', data: { citations: [{ marker: '[S1.1]', existence: 'ok', support: 'unchecked' }],
        pairs: { judged: 5, failed: 1, budget: 2 } }, body_ref: null },
      { seq: 7, type: 'run_finished', data: { status: 'succeeded' }, body_ref: null },
    ], next: null },
  };
  const asked = [];
  const events = await readTrace('r1', async (path) => { asked.push(path); return pages[new URL(path, 'http://x').searchParams.get('after')]; });
  assert.deepEqual(asked, ['/api/runs/r1/trace?after=-1', '/api/runs/r1/trace?after=1']);
  const found = summarize(events);
  assert.equal(found.route, 'openrouter:m');
  assert.deepEqual(found.retrieval, { route: 'literature', decided_by: 'fallback' });
  assert.deepEqual(found.search, { mode: 'hybrid', reason: null, passages: 2, rerank: 'fallback' });
  assert.deepEqual(found.steps.map((s) => [s.phase, s.estimate, s.cut, s.attempts.map((a) => [a.outcome, a.body])]),
    [['answer', 0.05, true, [['ok', 'aa']]], ['judge', 0.01, false, [['timeout', 'bb'], ['ok', null], ['ok', null]]]]);
  assert.deepEqual(found.steps[1].attempts.map((a) => [a.removed, a.unavailable]), [[false, false], [true, false], [false, true]]);
  assert.equal(found.checks.length, 1);
  assert.deepEqual(found.pairs, { judged: 5, failed: 1, budget: 2 }); // every outcome, shown in Details
  assert.equal(found.finished, 'succeeded');
});

test('papers chosen for the next message are one project\'s, kept once each, and a cited paper opens once', () => {
  let heard = 0;
  const stop = watchChosen(() => { heard += 1; });
  choosePapers('p1', [{ id: 'a', title: 'A' }, { id: 'b', title: 'B' }, { id: 'a', title: 'A' }]);
  assert.deepEqual(chosenPapers('p1').map((p) => p.id), ['a', 'b']);
  assert.deepEqual(chosenPapers('p2'), []); // another project's view chooses nothing
  assert.equal(chosenPapers('p1'), chosenPapers('p1')); // the same list while unchanged (useSyncExternalStore)
  choosePapers('p1', Array.from({ length: 60 }, (_, i) => ({ id: String(i) })));
  assert.equal(chosenPapers('p1').length, MAX_CHOSEN);
  choosePapers('p1', []);
  stop();
  assert.equal(heard, 3);
  openPaperAt('p1', 'm1', { id: 'p9', ordinal: 2 });
  assert.equal(takeOpening('p2'), null);
  assert.deepEqual(takeOpening('p1'), { id: 'm1', target: { id: 'p9', ordinal: 2 } });
  assert.equal(takeOpening('p1'), null); // once
});

test('every notice, chip and Details string is in both catalogs', () => {
  for (const key of Object.values(NOTICES)) assert.ok(en[key] && zhCN[key], key);
  const sources = ['../src/markdown.js', '../src/components/Trace.jsx', '../src/components/ConversationView.jsx',
    '../src/components/Library.jsx'].map((path) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8')).join('\n');
  const keys = [...sources.matchAll(/'((?:cite\.|trace\.|turn\.notice\.|library\.(?:choose|chosen|askAbout|chooseNone)|composer\.(?:chosen|unchoose|mentions|noMentions))[\w.]*)'/g)]
    .map((m) => m[1]).filter((key) => !key.endsWith('.'));
  assert.ok(keys.length > 30);
  for (const key of keys) assert.ok(Object.hasOwn(en, key) && Object.hasOwn(zhCN, key), key);
  for (const prefix of ['trace.route.', 'trace.decided.', 'trace.mode.']) { // keys built from a value
    assert.deepEqual(Object.keys(zhCN).filter((k) => k.startsWith(prefix)), Object.keys(en).filter((k) => k.startsWith(prefix)));
  }
  for (const value of ['literature', 'memory_literature', 'none', 'memory', 'find_papers']) assert.ok(en[`trace.route.${value}`], value);
  for (const value of ['rules', 'router', 'fallback']) assert.ok(en[`trace.decided.${value}`], value);
  for (const value of ['hybrid', 'keyword_only', 'selection']) assert.ok(en[`trace.mode.${value}`], value);
});
