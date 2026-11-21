// Design review Z: the background-run list (Settings, Advanced) after a deletion settles: it names each run's project and
// its papers' titles. Settings drawn alone, as the Shell draws it; every answer and the poll are the scenario's.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { loader } from './harness/load.mjs';

const load = await loader();
after(() => load.close());

const NAME = 'DELETED-PROJECT-CANARY'; const PAPER = 'DELETED-PAPER-TITLE-CANARY';
for (const strict of [false, true]) {
  test(`aDeletedProjectsRunsAreGoneFromTheRunList${strict ? ' (StrictMode)' : ''}`, async () => {
    const names = ['fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'window', 'document'];
    const kept = Object.fromEntries(names.map((n) => [n, Object.getOwnPropertyDescriptor(globalThis, n)]));
    try {
      const { mount, jsx, library } = await load.fresh();
      const polls = []; let activity = 200;
      const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
      globalThis.fetch = (path) => (path === '/api/settings' ? Promise.resolve(response(200, { hash: 'h', values: { models: {}, limits: {}, ui: {}, subagents: { models: [] } } }))
        : path === '/api/activity' ? Promise.resolve(activity === 200 ? response(200, { runs: [{ run_id: 'r1', workflow: 'lookup', status: 'succeeded',
          project_id: 'p1', project_kind: 'research', project_name: NAME, started_at: '2026-01-01T00:00:00Z', materials: { count: 1, titles: [PAPER] } }] })
          : response(activity, { code: 'unreachable' }))
          : new Promise(() => {}));
      globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {};
      globalThis.setInterval = (fn) => { polls.push(fn); return polls.length; }; globalThis.clearInterval = () => {};
      globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400 };
      globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
      const { Settings } = await load.component('Settings');
      const ui = await mount([jsx(Settings, { open: true, onOpenChange() {}, health: {}, project: { id: 'p2', name: 'Another', kind: 'research' }, onLanguage() {}, startPage: 'advanced' })], { strict }).settle();
      assert.ok(ui.text().includes(NAME) && ui.text().includes(PAPER), 'the run is listed');
      // the project is deleted (its request settles: Sidebar.jsx fires the one change event) and the next poll fails
      activity = 503;
      library.libraryChanged(null); await ui.settle();
      assert.ok(!ui.text().includes(PAPER) && !ui.text().includes(NAME), 'as the deletion settles');
      polls.forEach((fn) => fn()); await ui.settle();
      assert.ok(!ui.text().includes(PAPER) && !ui.text().includes(NAME), 'after the poll failed');
    } finally {
      for (const [n, d] of Object.entries(kept)) { if (d) Object.defineProperty(globalThis, n, d); else delete globalThis[n]; }
    }
  });
}

// The whole-batch review (F3): the read guard. A poll asked before the change and answering after it puts nothing back.
for (const strict of [false, true]) {
  test(`aPollAskedBeforeADeletionPutsNothingBack${strict ? ' (StrictMode)' : ''}`, async () => {
    const names = ['fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'window', 'document'];
    const kept = Object.fromEntries(names.map((n) => [n, Object.getOwnPropertyDescriptor(globalThis, n)]));
    try {
      const { mount, jsx, library } = await load.fresh();
      const polls = []; const held = []; let hold = false;
      const response = (status, body) => ({ ok: status < 400, status, json: async () => body });
      const listed = () => response(200, { runs: [{ run_id: 'r1', workflow: 'lookup', status: 'succeeded', project_id: 'p1',
        project_kind: 'research', project_name: NAME, started_at: '2026-01-01T00:00:00Z', materials: { count: 1, titles: [PAPER] } }] });
      globalThis.fetch = (path) => (path === '/api/settings' ? Promise.resolve(response(200, { hash: 'h', values: { models: {}, limits: {}, ui: {}, subagents: { models: [] } } }))
        : path === '/api/activity' ? (hold ? new Promise((resolve) => held.push(resolve)) : Promise.resolve(response(200, { runs: [] })))
          : new Promise(() => {}));
      globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {};
      globalThis.setInterval = (fn) => { polls.push(fn); return polls.length; }; globalThis.clearInterval = () => {};
      globalThis.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1400 };
      globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
      const { Settings } = await load.component('Settings');
      const ui = await mount([jsx(Settings, { open: true, onOpenChange() {}, health: {}, project: { id: 'p2', name: 'Another', kind: 'research' }, onLanguage() {}, startPage: 'advanced' })], { strict }).settle();
      hold = true;
      polls.forEach((fn) => fn()); await ui.settle(); // a poll asked before the deletion settles, not answered yet
      const before = held.length;
      hold = false;
      library.libraryChanged(null); await ui.settle(); // the deletion settles: the list is read again (answered empty)
      held.slice(0, before).forEach((resolve) => resolve(listed())); await ui.settle(); // the older poll answers late
      assert.ok(!ui.text().includes(PAPER) && !ui.text().includes(NAME), 'an older poll put the deleted project\'s run back');
    } finally {
      for (const [n, d] of Object.entries(kept)) { if (d) Object.defineProperty(globalThis, n, d); else delete globalThis[n]; }
    }
  });
}
