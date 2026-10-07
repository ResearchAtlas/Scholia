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
mkdirSync(out, { recursive: true });

const walk = (dir) => readdirSync(dir, { recursive: true }).filter((name) => statSync(join(dir, name)).isFile());
const pngs = walk(baseline).filter((name) => name.endsWith('.png') && !name.endsWith('-failed.png')).sort();

const browser = await chromium.launch({ executablePath: process.env.SCHOLIA_BROWSER ?? BROWSER,
  args: ['--host-resolver-rules=MAP * ~NOTFOUND'] });
const page = await browser.newPage();
const summary = { baseline, candidate, screenshots: [], motion: [] };
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
  for (const key of Object.keys(ma)) {
    const [x, y] = [ma[key], mb[key]];
    if (x?.opened !== undefined) {
      for (const phase of ['opened', 'closed']) {
        if (!x[phase] !== !y?.[phase]) { differences.push(`${key}.${phase}: present in one run only`); continue; }
        if (!x[phase]) continue;
        if (x[phase].duration !== y[phase].duration) differences.push(`${key}.${phase}.duration ${x[phase].duration} → ${y[phase].duration}`);
        x[phase].frames.forEach((f, i) => {
          const g = y[phase].frames[i];
          for (const k of ['x', 'y', 'width', 'height']) if (Math.abs(f[k] - g[k]) > 0.5) differences.push(`${key}.${phase} t=${f.t} ${k} ${f[k].toFixed(1)} → ${g[k].toFixed(1)}`);
          if (Math.abs(f.opacity - g.opacity) > 0.01) differences.push(`${key}.${phase} t=${f.t} opacity ${f.opacity} → ${g.opacity}`);
        });
      }
    } else if (JSON.stringify(x) !== JSON.stringify(y)) {
      differences.push(`${key}: ${JSON.stringify(x)} → ${JSON.stringify(y)}`);
    }
  }
  summary.motion.push({ name, differences });
}

writeFileSync(join(out, 'summary.json'), JSON.stringify(summary, null, 2));
const changed = summary.screenshots.filter((s) => s.count || s.missing || s.size);
console.log(`${summary.screenshots.length} screenshots compared, ${changed.length} differ`);
for (const s of changed) console.log(`  ${s.name}: ${s.missing ? 'missing' : s.size ? `size ${JSON.stringify(s.size)}` : `${s.count} px in ${JSON.stringify(s.box)}`}`);
for (const m of summary.motion) console.log(`motion ${m.name}: ${m.missing ? 'missing' : m.differences.length ? m.differences.join('; ') : 'same'}`);

