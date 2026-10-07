// Compares two walkthrough runs: each pair of screenshots pixel by pixel, and the sampled motion.
//
//     node tests/walkthrough_compare.mjs BASELINE CANDIDATE OUT
//
// BASELINE and CANDIDATE are folders written by walkthrough_driver.mjs (with --all, one folder per
// combination). For each screenshot in both, OUT gets the count of pixels whose color differs
// (any channel by more than 2 of 255) and, where any differ, a diff image: the candidate dimmed,
// differing pixels in red. Motion is compared frame by frame (box within 0.5 px, opacity within
// 0.01). OUT/summary.json holds every result; nothing is accepted or replaced here, each
// difference is for review.

import { readFileSync, readdirSync, existsSync, mkdirSync, writeFileSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const { chromium } = createRequire(join(ROOT, 'frontend', 'package.json'))('playwright-core');
const BROWSER = join(homedir(), 'Library/Caches/ms-playwright/chromium-1217/chrome-mac-arm64',
  'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing');
const [baseline, candidate, out] = process.argv.slice(2);
if (!out) throw new Error('usage: walkthrough_compare.mjs BASELINE CANDIDATE OUT');
// Every file in OUT must come from this comparison: a folder that already holds files is refused.
if (existsSync(out) && readdirSync(out).length) throw new Error(`${out} is not empty: give each comparison a new folder`);
mkdirSync(out, { recursive: true });

const walk = (dir) => readdirSync(dir, { recursive: true }).filter((name) => statSync(join(dir, name)).isFile());
const pngs = walk(baseline).filter((name) => name.endsWith('.png') && !name.endsWith('-failed.png')).sort();
// Each file below is compared from the baseline's list; one only the candidate has is a difference too.
const compared = (name) => (name.endsWith('.png') && !name.endsWith('-failed.png')) || name.endsWith('motion.json') || name.endsWith('.styles.json');
const baselineFiles = new Set(walk(baseline));

const browser = await chromium.launch({ executablePath: process.env.SCHOLIA_BROWSER ?? BROWSER,
  args: ['--host-resolver-rules=MAP * ~NOTFOUND'] });
const page = await browser.newPage();
const summary = { baseline, candidate, screenshots: [], motion: [],
                  candidateOnly: walk(candidate).filter((name) => compared(name) && !baselineFiles.has(name)).sort() };
for (const name of pngs) {
  if (!existsSync(join(candidate, name))) { summary.screenshots.push({ name, missing: true }); continue; }
  const [a, b] = [baseline, candidate].map((dir) => readFileSync(join(dir, name)).toString('base64'));
  const result = await page.evaluate(async ({ a, b }) => {
    const load = (data) => new Promise((resolve, reject) => {
      const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = `data:image/png;base64,${data}`;
    });
    const [ia, ib] = await Promise.all([load(a), load(b)]);
    if (ia.width !== ib.width || ia.height !== ib.height) return { size: [[ia.width, ia.height], [ib.width, ib.height]] };
    const pixels = (image) => {
      const canvas = new OffscreenCanvas(image.width, image.height); const c = canvas.getContext('2d');
      c.drawImage(image, 0, 0); return c.getImageData(0, 0, image.width, image.height);
    };
    const [pa, pb] = [pixels(ia), pixels(ib)];
    const diff = new ImageData(ia.width, ia.height);
    let count = 0; const box = { x0: Infinity, y0: Infinity, x1: -1, y1: -1 };
    for (let i = 0; i < pa.data.length; i += 4) {
      const differs = [0, 1, 2, 3].some((k) => Math.abs(pa.data[i + k] - pb.data[i + k]) > 2);
      const p = i / 4; const x = p % ia.width; const y = Math.floor(p / ia.width);
      if (differs) {
        count += 1; diff.data.set([255, 0, 0, 255], i);
        box.x0 = Math.min(box.x0, x); box.y0 = Math.min(box.y0, y); box.x1 = Math.max(box.x1, x); box.y1 = Math.max(box.y1, y);
      } else {
        diff.data.set([pb.data[i] / 3 + 170, pb.data[i + 1] / 3 + 170, pb.data[i + 2] / 3 + 170, 255], i);
      }
    }
    if (!count) return { count };
    const canvas = new OffscreenCanvas(ia.width, ia.height); canvas.getContext('2d').putImageData(diff, 0, 0);
    const blob = await canvas.convertToBlob({ type: 'image/png' });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
    return { count, box, image: btoa(binary) };
  }, { a, b });
  const entry = { name, count: result.count, box: result.box, size: result.size };
  if (result.image) {
    entry.diff = `diff/${name}`;
    mkdirSync(dirname(join(out, entry.diff)), { recursive: true });
    writeFileSync(join(out, entry.diff), Buffer.from(result.image, 'base64'));
  }
  summary.screenshots.push(entry);
}
await browser.close();

for (const name of walk(baseline).filter((n) => n.endsWith('motion.json'))) {
  if (!existsSync(join(candidate, name))) { summary.motion.push({ name, missing: true }); continue; }
  const [ma, mb] = [baseline, candidate].map((dir) => JSON.parse(readFileSync(join(dir, name), 'utf8')));
  const differences = [];
  // Every key and frame either run recorded: one only the candidate has is a difference too.
  for (const key of [...new Set([...Object.keys(ma), ...Object.keys(mb)])]) {
    const [x, y] = [ma[key], mb[key]];
    if (x === undefined || y === undefined) { differences.push(`${key}: present in one run only`); continue; }
    if (x?.opened !== undefined) {
      for (const phase of ['opened', 'closed']) {
        if (!x[phase] !== !y?.[phase]) { differences.push(`${key}.${phase}: present in one run only`); continue; }
        if (!x[phase]) continue;
        if (x[phase].immediate || y[phase].immediate) {
          if (!x[phase].immediate !== !y[phase].immediate) differences.push(`${key}.${phase}: immediate in one run only`);
          continue;
        }
        if (x[phase].duration !== y[phase].duration) differences.push(`${key}.${phase}.duration ${x[phase].duration} → ${y[phase].duration}`);
        if (x[phase].frames.length !== y[phase].frames.length) { differences.push(`${key}.${phase}: ${x[phase].frames.length} → ${y[phase].frames.length} frames`); continue; }
        x[phase].frames.forEach((f, i) => {
          const g = y[phase].frames[i];
          for (const k of ['x', 'y', 'width', 'height']) if (Math.abs(f[k] - g[k]) > 0.5) differences.push(`${key}.${phase} t=${f.t} ${k} ${f[k].toFixed(1)} → ${g[k].toFixed(1)}`);
          if (Math.abs(f.opacity - g.opacity) > 0.01) differences.push(`${key}.${phase} t=${f.t} opacity ${f.opacity} → ${g.opacity}`);
        });
      }
    } else if (key === 'reducedMotion') {
      // How many animations run at once varies; what matters is which elements were checked and that
      // every animation lasted at most 1 ms.
      // A closing element can be gone before it is read, so closing entries are not compared by presence.
      const keys = (m) => Object.keys(m ?? {}).filter((k) => !k.endsWith(' closing')).sort().join(', ');
      if (keys(x) !== keys(y)) differences.push(`reducedMotion: checked ${keys(x)} → ${keys(y)}`);
      const over = Object.entries(y ?? {}).filter(([, list]) => list.some((d) => (d?.duration ?? d) > 1)).map(([k]) => k);
      if (over.length) differences.push(`reducedMotion: over 1 ms in ${over.join(', ')}`);
    } else if (JSON.stringify(x) !== JSON.stringify(y)) {
      differences.push(`${key}: ${JSON.stringify(x)} → ${JSON.stringify(y)}`);
    }
  }
  summary.motion.push({ name, differences });
}

// Computed styles (--styles), element by element in document order: each changed property, counted
// by its old and new value, with the first element it was seen on. Values that render the same
// are compared as one: a color in oklab and in rgb (within 1 of 255 a channel), shadows without
// their transparent layers, default gradient stops, any radius of 9999px or more, and the outline
// properties of an outline drawn in neither run (a drawn one is compared in full). Margins, transforms and translations are not compared as values: the element's
// box (within 0.5 px) shows what they do. Elements that take no space are skipped.
function oklabToRgb(l, a, b) {
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (l - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  const linear = [4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_];
  return linear.map((c) => 255 * (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055));
}
function normalize(key, value) {
  if (typeof value !== 'string') return value;
  let v = value.replace(/oklab\(([-\d.e]+) ([-\d.e]+) ([-\d.e]+)(?: \/ ([\d.]+))?\)/g, (_, l, a, b, alpha) => {
    const [r, g, bl] = oklabToRgb(Number(l), Number(a), Number(b)).map((c) => Math.round(Math.min(255, Math.max(0, c))));
    return alpha === undefined ? `rgb(${r}, ${g}, ${bl})` : `rgba(${r}, ${g}, ${bl}, ${Number(alpha)})`;
  });
  if (key === 'box-shadow') v = v.split(/,(?![^(]*\))/).map((x) => x.trim()).filter((x) => x !== 'rgba(0, 0, 0, 0) 0px 0px 0px 0px').join(', ') || 'none';
  if (key === 'background-image') v = v.replace(/ (0|50|100)%(?=[,)])/g, '');
  if (key.endsWith('radius') && parseFloat(v) >= 9999) v = 'full';
  return v;
}
function sameColor(x, y) {
  const parse = (v) => (v.match(/[\d.]+/g) ?? []).map(Number);
  const [a, b] = [parse(x), parse(y)];
  return a.length === b.length && a.every((n, i) => Math.abs(n - b[i]) <= (i === 3 ? 0.01 : 1.01));
}
const SKIP = /^(margin|transform|translate|scale)/;
const drawnOutline = (r) => r['outline-style'] !== 'none' && !/rgba\([^)]*, 0\)$/.test(r['outline-color']);
summary.styles = {};
for (const name of walk(baseline).filter((n) => n.endsWith('.styles.json'))) {
  if (!existsSync(join(candidate, name))) { summary.styles[name] = 'missing'; continue; }
  const [sa, sb] = [baseline, candidate].map((dir) => JSON.parse(readFileSync(join(dir, name), 'utf8')));
  const [pa, pb] = [sa, sb].map((records) => new Map(records.map((r) => [r.path, r])));
  const changes = {};
  const note = (change, path) => { (changes[change] ??= { count: 0, example: path }).count += 1; };
  if (sa.length !== sb.length) note(`element count ${sa.length} → ${sb.length}`, '');
  sa.forEach((x, i) => {
    const y = sb[i];
    if (!y || y.path !== x.path) return note('element order', x.path);
    if (x.box[2] === 0 && x.box[3] === 0 && y.box[2] === 0 && y.box[3] === 0) return;
    // Position within the parent, and size: a change shows on the element that moved, not on
    // everything after it.
    const relative = (record, byPath) => {
      const parent = byPath.get(record.path.split('/').slice(0, -1).join('/'));
      return [record.box[0] - (parent?.box[0] ?? 0), record.box[1] - (parent?.box[1] ?? 0), record.box[2], record.box[3]];
    };
    const [rx, ry] = [relative(x, pa), relative(y, pb)];
    if (rx.some((v, k) => Math.abs(v - ry[k]) > 0.5)) note(`box ${rx.map((v) => Math.round(v * 10) / 10)} → ${ry.map((v) => Math.round(v * 10) / 10)}`, x.path);
    if (drawnOutline(x) !== drawnOutline(y)) note(`outline drawn ${drawnOutline(x)} → ${drawnOutline(y)}`, x.path);
    if (drawnOutline(x) || drawnOutline(y)) {  // a drawn outline is compared in full
      for (const key of ['outline-style', 'outline-width', 'outline-color', 'outline-offset']) {
        const [a, b] = [normalize(key, x[key]), normalize(key, y[key])];
        if (a !== b && !(key === 'outline-color' && sameColor(a, b))) note(`${key}: ${a} → ${b}`, x.path);
      }
    }
    for (const key of new Set([...Object.keys(x), ...Object.keys(y)])) {
      if (key === 'path' || key === 'box' || key.startsWith('outline') || SKIP.test(key)) continue;
      const [a, b] = [normalize(key, x[key]), normalize(key, y[key])];
      if (a === b || (/color|placeholder/.test(key) && sameColor(a, b))) continue;
      note(`${key}: ${a} → ${b}`, x.path);
    }
  });
  summary.styles[name] = changes;
}
const styleChanges = {};
for (const [name, changes] of Object.entries(summary.styles)) {
  if (changes === 'missing') continue;
  for (const [change, { count, example }] of Object.entries(changes)) {
    (styleChanges[change] ??= { count: 0, files: 0, example: `${name} ${example}` }).count += count;
    styleChanges[change].files += 1;
  }
}
summary.styleChanges = styleChanges;

writeFileSync(join(out, 'summary.json'), JSON.stringify(summary, null, 2));
const changed = summary.screenshots.filter((s) => s.count || s.missing || s.size);
console.log(`${summary.screenshots.length} screenshots compared, ${changed.length} differ`);
for (const s of changed) console.log(`  ${s.name}: ${s.missing ? 'missing' : s.size ? `size ${JSON.stringify(s.size)}` : `${s.count} px in ${JSON.stringify(s.box)}`}`);
for (const [change, { count, files, example }] of Object.entries(styleChanges)) console.log(`style ${change} (${count} elements in ${files} steps; ${example})`);
console.log(`${summary.candidateOnly.length} files only in the candidate${summary.candidateOnly.map((name) => `\n  ${name}`).join('')}`);
for (const m of summary.motion) console.log(`motion ${m.name}: ${m.missing ? 'missing' : m.differences.length ? m.differences.join('; ') : 'same'}`);

