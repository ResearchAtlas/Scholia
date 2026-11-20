// The Library's late-feedback scenarios (PR 61, design review O's appendix A3, Option 1 with rider K): the real Library,
// AddMenu and Paper bodies (tests/harness/load.mjs, through Vite) on a stand-in for React with mount and unmount
// (tests/harness/standin.mjs), answers deferred and released in a fixed order, no timers, each scenario in a fresh module
// graph, plain and with StrictMode's second effect run. Each asserts on what the drawn tree says (a status, an alert, a
// field's value, a button's disabled), never on source text. Each fails on the commit before its fix: r4 on c0861d7, the
// r5 set on 98a8302, r6, r6fail and the three chip scenarios on 9d0bff7; redraw, r6alone, project and panel are guards.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';
import { world, imports, uploads, saves, rereads, report } from './harness/world.mjs';

const load = await loader();
after(() => load.close());
const SKIPPED = /notes\.txt/;

const scenarios = {
  // Round 4: a late replacement failure for paper A is never shown on paper B.
  async r4(w) {
    await w.click('Paper A');
    await w.choose('paper-replace', w.file('a.pdf'));
    await w.click('Library');
    await w.click('Paper B');
    await w.release(w.pending(uploads)[0], (r) => r.fail('unreachable', 503));
    assert.deepEqual(w.says('status').filter((s) => !/Adding/.test(s)), []);
    assert.deepEqual(w.says('alert'), []);
  },
  // Round 4, the other half: it is shown on A's own page when that page is the one open as it answers.
  async r4own(w) {
    await w.click('Paper A');
    await w.choose('paper-replace', w.file('a.pdf'));
    await w.click('Library');
    await w.click('Paper A');
    await w.release(w.pending(uploads)[0], (r) => r.fail('unreachable', 503));
    assert.equal(w.says('status').filter((s) => /engine is not responding/.test(s)).length, 1);
  },
  // Round 5, orders 1 and 3: replacing A; B opened and a file Scholia does not read chosen there; then A fails.
  async r5other(w) {
    await w.click('Paper A');
    await w.choose('paper-replace', w.file('a.pdf'));
    await w.click('Library');
    await w.click('Paper B');
    await w.choose('paper-replace', w.file('notes.txt'));
    assert.equal(w.says('status').filter((s) => SKIPPED.test(s)).length, 1);
    await w.release(w.pending(uploads)[0], (r) => r.fail('unreachable', 503));
    assert.deepEqual(w.says('status').filter((s) => !/Adding/.test(s)).map((s) => SKIPPED.test(s)), [true]);
  },
  // Round 5, order 2: replacing A; Back; A again and a second choice; the first fails after it, the second succeeds.
  async r5same(w) {
    await w.click('Paper A');
    await w.choose('paper-replace', w.file('a1.pdf'));
    await w.click('Library');
    await w.click('Paper A');
    await w.choose('paper-replace', w.file('a2.pdf'));
    await w.release(w.pending(uploads)[0], (r) => r.fail('unreachable', 503));
    assert.deepEqual(w.says('status').filter((s) => /engine/.test(s)), []);
    assert.equal(w.pending(uploads).length, 2); // the second's turn came
    await w.release(w.pending(uploads)[1], (r) => r.answer({ materials: [{ id: 'a' }], lookup_run_id: null }));
    assert.deepEqual(w.says('status').filter((s) => /engine/.test(s)), []);
    assert.ok(w.shows('Replace file'));
  },
  // Round 5, order 4: saves on A and on B; B answers first, then A; B's Saved line stays.
  async r5saved(w) {
    await w.click('Paper A'); await w.type('title', 'A, corrected'); await w.save();
    await w.click('Library');
    await w.click('Paper B'); await w.type('title', 'B, corrected'); await w.save();
    const [a, b] = w.pending(saves);
    w.papers[1].title = 'B, corrected'; w.papers[1].updated_at = 't2';
    await w.release(b, (r) => r.answer({ ok: true }));
    assert.equal(w.says('status').filter((s) => /^Saved/.test(s)).length, 1);
    await w.release(a, (r) => r.answer({ ok: true }));
    assert.equal(w.says('status').filter((s) => /^Saved/.test(s)).length, 1);
  },
  // Batch 5 review P3-1: a newer save's failure stays when an older save's success draws the page again.
  async r5fail(w) {
    await w.click('Paper A'); await w.type('title', 'First'); await w.save();
    await w.changed(); // another change draws the page again: its form is new, its Save free
    await w.type('title', 'Second'); await w.save();
    const [first, second] = w.pending(saves);
    await w.release(second, (r) => r.fail('invalid_request', 422));
    assert.equal(w.says('alert').length, 1);
    await w.release(first, (r) => r.answer({ ok: true }));
    assert.equal(w.says('alert').length, 1);
    assert.deepEqual(w.says('status').filter((s) => /^Saved/.test(s)), []);
  },
  // Round 6: an import, a paper opened and left (a new Add menu), a second import that answers, then the first.
  async r6(w) {
    await w.choose('library-reference-file', w.file('first.ris'));
    await w.click('Paper A');
    await w.click('Library');
    await w.choose('library-reference-file', w.file('second.ris'));
    const [first, second] = w.pending(imports);
    await w.release(second, report(2));
    assert.deepEqual(w.says('status'), ['Imported 2 papers.']);
    await w.release(first, (r) => r.fail());
    assert.deepEqual(w.says('status'), ['Imported 2 papers.']);
  },
  // Round 6, the other answer: the first import's success never replaces the second's failure.
  async r6fail(w) {
    await w.choose('library-reference-file', w.file('first.ris'));
    await w.click('Paper A');
    await w.click('Library');
    await w.choose('library-reference-file', w.file('second.ris'));
    const [first, second] = w.pending(imports);
    await w.release(second, (r) => r.fail());
    await w.release(first, report(3));
    assert.deepEqual(w.says('status'), ['Something went wrong. Try again.']);
  },
  // An import alone still reports after the Add menu was drawn again.
  async r6alone(w) {
    await w.choose('library-reference-file', w.file('first.ris'));
    await w.click('Paper A');
    await w.click('Library');
    await w.release(w.pending(imports)[0], report(3));
    assert.deepEqual(w.says('status'), ['Imported 3 papers.']);
  },
  // The requirement: nothing of the paper while the papers are read again, and the Saved line after it.
  async redraw(w) {
    await w.click('Paper A'); await w.type('title', 'A, corrected'); await w.save();
    w.hold.listing = true;
    await w.release(w.pending(saves)[0], (r) => r.answer({ ok: true }));
    assert.equal(w.shows('Paper A'), false);
    assert.ok(w.says('status').includes('Loading…') || w.says('status').some((s) => /Loading/.test(s)));
    w.hold.listing = false; w.papers[0].title = 'A, corrected'; w.papers[0].updated_at = 't2';
    await w.release(w.asked.findLast((r) => r.method === 'GET'), (r) => r.answer({ materials: structuredClone(w.papers), asks: [] }));
    assert.equal(w.says('status').filter((s) => /^Saved/.test(s)).length, 1);
    assert.equal(w.value('title'), 'A, corrected');
  },
  // A citation opens B while A's page is open: nothing of A's page is B's.
  async chipReread(w) {
    await w.click('Paper A'); await w.click('Read again'); await w.chip('b');
    await w.release(w.pending(rereads)[0], (r) => r.fail('unreachable', 503));
    assert.deepEqual(w.says('alert'), []);
  },
  async chipEdit(w) {
    await w.click('Paper A'); await w.type('title', 'Typed for A'); await w.chip('b');
    assert.equal(w.value('title'), 'Paper B');
    assert.equal(w.enabled('Save'), false);
  },
  async chipBusy(w) {
    await w.click('Paper A'); await w.choose('paper-replace', w.file('a.pdf')); await w.chip('b');
    assert.equal(w.enabled('Replace file'), true);
  },
  // Another project opened while an import runs: its answer is not the other project's, and the change is still told.
  async project(w) {
    await w.choose('library-reference-file', w.file('first.ris'));
    await w.panel('p2');
    const before = w.library.libraryGeneration();
    await w.release(w.pending(imports)[0], report(3));
    assert.deepEqual(w.says('status'), []);
    assert.equal(w.library.libraryGeneration(), before + 1);
    await w.panel('p1');
    assert.deepEqual(w.says('status'), []); // back in the first project: the old Library's answer is not the new one's
  },
  // The panel closed and opened again while a replacement runs: the late failure, then a new replacement's notice.
  async panel(w) {
    await w.click('Paper A'); await w.choose('paper-replace', w.file('a.pdf'));
    await w.panel(null); await w.panel('p1'); await w.click('Paper A');
    await w.release(w.pending(uploads)[0], (r) => r.fail('unreachable', 503));
    assert.deepEqual(w.says('status').filter((s) => !/Adding/.test(s)), []); // the late failure is not shown
    await w.choose('paper-replace', w.file('notes.txt'));
    assert.equal(w.says('status').filter((s) => SKIPPED.test(s)).length, 1);
  },
};

for (const [name, run] of Object.entries(scenarios)) {
  for (const strict of [false, true]) {
    test(`${name}${strict ? ' (StrictMode)' : ''}`, async () => {
      const kept = { fetch: globalThis.fetch, FileReader: globalThis.FileReader, setTimeout: globalThis.setTimeout,
        clearTimeout: globalThis.clearTimeout, setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval };
      try {
        await run(await world(await load.fresh(), { strict }));
      } finally {
        Object.assign(globalThis, kept);
      }
    });
  }
}
