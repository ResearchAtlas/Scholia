// The interface script loaded at start, against docs/interface-criteria.md's Performance criterion:
// under 600 kB minified. The script loaded at start is what the built page names (its module script
// and the scripts it modulepreloads) and every chunk those import statically; a chunk reached only
// through import() loads when its part of the window is first opened. Prints each chunk's size, in
// kB of 1,000 bytes as Vite prints them, and exits 1 when the start script is over the limit or the
// build cannot be read.
//
//     node tools/script_size.mjs [DIST]      (DIST defaults to frontend/dist)
import { readFileSync, readdirSync } from 'node:fs';
import { join, posix } from 'node:path';
import { fileURLToPath } from 'node:url';

export const LIMIT = 600_000;

// import ... from "./x.js", import "./x.js" and export ... from "./x.js", but not import("./x.js").
const STATIC = /\b(?:import|export)\s*(?:[\w$\s{},*]*?\bfrom\s*)?["'](\.\.?\/[^"']+)["']/g;

const tags = (page, name) => [...page.matchAll(new RegExp(`<${name}\\b[^>]*>`, 'gi'))].map((match) => match[0]);
const attribute = (tag, name) => tag.match(new RegExp(`\\b${name}\\s*=\\s*"([^"]*)"`, 'i'))?.[1];

// Every script of the build, by path under dist with its size in bytes, and whether it loads at start.
export function scripts(dist) {
  const page = readFileSync(join(dist, 'index.html'), 'utf8');
  const scripts = tags(page, 'script').filter((tag) => attribute(tag, 'src'));
  // The window starts from a module script; a page that only preloads would start nothing.
  if (!scripts.some((tag) => attribute(tag, 'type') === 'module')) throw new Error('index.html names no module script');
  const named = [
    ...scripts.map((tag) => attribute(tag, 'src')),
    ...tags(page, 'link').filter((tag) => attribute(tag, 'rel') === 'modulepreload').map((tag) => attribute(tag, 'href')),
  ].filter(Boolean).map((path) => path.replace(/^\//, ''));
  const start = new Set();
  const visit = (path) => {
    if (start.has(path)) return;
    start.add(path);
    const code = readFileSync(join(dist, path), 'utf8');
    for (const [, imported] of code.matchAll(STATIC)) visit(posix.join(posix.dirname(path), imported));
  };
  named.forEach(visit);
  const all = new Set([...start, ...readdirSync(join(dist, 'assets')).filter((name) => name.endsWith('.js'))
    .map((name) => `assets/${name}`)]);
  return [...all].map((path) => ({ path, bytes: readFileSync(join(dist, path)).length, start: start.has(path) }))
    .sort((a, b) => b.start - a.start || b.bytes - a.bytes || a.path.localeCompare(b.path));
}

const kB = (bytes) => `${(bytes / 1000).toFixed(2)} kB`;

function main(dist) {
  let found;
  try {
    found = scripts(dist);
  } catch (error) {
    console.error(`The build in ${dist} could not be read: ${error.message}`);
    return 1;
  }
  console.log(`The interface script in ${dist}, minified:`);
  for (const { path, bytes, start } of found) {
    console.log(`  ${(start ? 'at start' : 'on demand').padEnd(10)} ${kB(bytes).padStart(10)}  ${path}`);
  }
  const total = found.filter((script) => script.start).reduce((sum, script) => sum + script.bytes, 0);
  if (total > LIMIT) {
    console.log(`Loaded at start: ${kB(total)}, over the ${kB(LIMIT)} limit (docs/interface-criteria.md, Performance).`);
    return 1;
  }
  console.log(`Loaded at start: ${kB(total)}, within the ${kB(LIMIT)} limit (${kB(LIMIT - total)} to spare).`);
  return 0;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv[2] ?? fileURLToPath(new URL('../frontend/dist', import.meta.url)));
}
