// The interface criteria's contrast rule (docs/interface-criteria.md): text on the surfaces
// it is used on meets WCAG 2.2 AA (4.5:1), and the focus ring meets 3:1, in light and dark.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

function tokens(selector) {
  const block = css.slice(css.indexOf(`${selector} {`), css.indexOf('}', css.indexOf(`${selector} {`)));
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*([\d.]+) ([\d.]+)% ([\d.]+)%/g)]
    .map(([, name, h, s, l]) => [name, [Number(h), Number(s) / 100, Number(l) / 100]]));
}

function luminance([h, s, l]) {
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const channel = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
  const linear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const [r, g, b] = [channel(0), channel(8), channel(4)].map(linear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const SURFACES = ['background', 'sidebar', 'card', 'popover', 'muted', 'accent'];
const TEXT = [
  ...SURFACES.map((surface) => ['foreground', surface]),
  ...SURFACES.map((surface) => ['muted-foreground', surface]),
  ...['background', 'card', 'brand-soft'].map((surface) => ['brand', surface]),
  ['foreground', 'brand-soft'], ['muted-foreground', 'brand-soft'], // the shared confirmation's question and note
  ...['background', 'card', 'popover'].map((surface) => ['destructive', surface]),
  ['warning', 'background'], ['warning', 'card'], ['success', 'background'],
  ['brand-foreground', 'brand'], ['primary-foreground', 'primary'],
  ['secondary-foreground', 'secondary'], ['destructive-foreground', 'destructive'],
];

for (const [mode, selector] of [['light', ':root'], ['dark', '.dark']]) {
  const colors = tokens(selector);
  test(`text meets 4.5:1 and the focus ring 3:1 in ${mode} mode`, () => {
    for (const [text, surface] of TEXT) {
      const ratio = contrast(colors[text], colors[surface]);
      assert.ok(ratio >= 4.5, `${mode}: ${text} on ${surface} is ${ratio.toFixed(2)}:1`);
    }
    for (const surface of ['background', 'sidebar', 'card']) {
      const ratio = contrast(colors.ring, colors[surface]);
      assert.ok(ratio >= 3, `${mode}: the ring on ${surface} is ${ratio.toFixed(2)}:1`);
    }
  });
}

test('the contrast formula matches known values', () => {
  assert.equal(Math.round(contrast([0, 0, 0], [0, 0, 1]) * 100) / 100, 21);
  assert.equal(Math.round(contrast([0, 0, 0.4627], [0, 0, 1]) * 100) / 100, 4.54); // #767676 on white
});
