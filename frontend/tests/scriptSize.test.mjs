// tools/script_size.mjs over a synthetic build: the script loaded at start is what the page names
// (its scripts and modulepreloads) and every chunk those import statically, never a chunk reached
// only through import(); over 600 kB it fails, and a build it cannot read fails too.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { LIMIT, scripts } from '../../tools/script_size.mjs';

const SCRIPT = fileURLToPath(new URL('../../tools/script_size.mjs', import.meta.url));
const PAGE = '<script type="module" crossorigin src="/assets/entry.js"></script>\n'
  + '<link rel="modulepreload" crossorigin href="/assets/vendor.js">\n<link rel="stylesheet" crossorigin href="/assets/app.css">';

// A build whose files hold the given code, each padded to its size in bytes when one is given.
function build(files, page = PAGE) {
  const dist = mkdtempSync(join(tmpdir(), 'scholia-script-size-'));
  mkdirSync(join(dist, 'assets'));
  writeFileSync(join(dist, 'index.html'), page);
  for (const [name, [code, size]] of Object.entries(files)) {
    writeFileSync(join(dist, 'assets', name), size ? code + ' '.repeat(size - Buffer.byteLength(code)) : code);
  }
  return dist;
}

const run = (dist) => spawnSync(process.execPath, [SCRIPT, dist], { encoding: 'utf8' });

const FILES = {
  'entry.js': ['import{a as b}from"./shared.js";import"./side.js";export{c}from"./more.js";const p=()=>import("./later.js");'],
  'shared.js': ['export const a=1;'],
  'side.js': ['import*as s from"./shared.js";'],
  'more.js': ['export const c=2;'],
  'vendor.js': ['export const v=3;'],
  'later.js': ['import{a}from"./shared.js";import{n}from"./nested.js";const q=()=>import("./deeper.js");'],
  'nested.js': ['export const n=4;'],
  'deeper.js': ['export const d=5;'],
  'app.css': ['body{}'],
};

test('static imports and modulepreloads load at start, import() only on demand', () => {
  const dist = build(FILES);
  try {
    const found = Object.fromEntries(scripts(dist).map((script) => [script.path, script.start]));
    assert.deepEqual(found, {
      'assets/entry.js': true, 'assets/shared.js': true, 'assets/side.js': true, 'assets/more.js': true, 'assets/vendor.js': true,
      'assets/later.js': false, 'assets/nested.js': false, 'assets/deeper.js': false,
    });
    const result = run(dist);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /at start .* assets\/entry\.js/);
    assert.match(result.stdout, /on demand .* assets\/later\.js/);
    assert.match(result.stdout, /Loaded at start: 0\.\d\d kB, within the 600\.00 kB limit/);
  } finally {
    rmSync(dist, { recursive: true, force: true });
  }
});

test('the start script passes one byte under 600 kB and fails at 600 kB, whatever waits on demand', () => {
  const startBytes = (dist) => scripts(dist).filter((s) => s.start).reduce((sum, s) => sum + s.bytes, 0);
  for (const [over, status] of [[-1, 0], [0, 1]]) {
    const others = ['entry.js', 'shared.js', 'side.js', 'more.js'].reduce((sum, name) => sum + Buffer.byteLength(FILES[name][0]), 0);
    const dist = build({ ...FILES, 'vendor.js': [FILES['vendor.js'][0], LIMIT - others + over], 'deeper.js': ['', 900_000] });
    try {
      assert.equal(startBytes(dist), LIMIT + over);
      const result = run(dist);
      assert.equal(result.status, status, result.stdout + result.stderr);
      assert.match(result.stdout, status ? /not under the 600\.00 kB limit/ : /within the 600\.00 kB limit \(0\.00 kB to spare\)/);
    } finally {
      rmSync(dist, { recursive: true, force: true });
    }
  }
});

test('a build that cannot be read fails: a named script missing, or no module script to start from', () => {
  for (const [files, page, reason] of [
    [{ ...FILES, 'side.js': undefined }, PAGE, /side\.js/],
    [FILES, '<link rel="stylesheet" href="/assets/app.css">', /names no module script/],
    // preloads alone start nothing, however small
    [FILES, '<link rel="modulepreload" href="/assets/vendor.js">\n<link rel="stylesheet" href="/assets/app.css">', /names no module script/],
    [FILES, '<script src="/assets/entry.js"></script>', /names no module script/],
  ]) {
    const dist = build(Object.fromEntries(Object.entries(files).filter(([, value]) => value)), page);
    try {
      const result = run(dist);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /could not be read/);
      assert.match(result.stderr, reason);
    } finally {
      rmSync(dist, { recursive: true, force: true });
    }
  }
});
