// A Vite plugin that records the npm packages a build bundles, for the license audit
// (tools/license_audit.py). It writes dist-licenses/packages.json, each bundled package's
// name, version, license, license files and install path (every version that ships, so two
// versions of one package are two entries), and copies the files to <name>@<version>/. Only modules the
// bundle contains count, so development tools never appear, apart from the packages whose
// code the stylesheet copies: Tailwind's base styles and tailwindcss-animate's keyframes.
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const NOTICE = /^(licen[cs]e|copying|notice)(\.|-|$)/i;
const IN_STYLES = ['tailwindcss', 'tailwindcss-animate'];

// The package a module id belongs to: the folder after its last node_modules.
export function packageRoot(id) {
  const path = id.replace(/^\0/, '').split('?')[0].replace(/\\/g, '/');
  const at = path.lastIndexOf('/node_modules/');
  if (at === -1) return null;
  const rest = path.slice(at + '/node_modules/'.length).split('/');
  const name = rest[0].startsWith('@') ? `${rest[0]}/${rest[1]}` : rest[0];
  return { name, root: path.slice(0, at + '/node_modules/'.length) + name };
}

function describe(root) {
  const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  const license = typeof manifest.license === 'string' ? manifest.license : manifest.license?.type ?? null;
  const files = readdirSync(root).filter((name) => NOTICE.test(name)).sort();
  const path = relative(HERE, root).replace(/\\/g, '/');
  return { name: manifest.name, version: manifest.version, license, files, path };
}

// The manifest of the packages whose roots a bundle's modules lie in, one entry per name and
// version, copying their license files under out/<name>@<version>/.
export function record(roots, out) {
  const packages = new Map();
  for (const root of roots) {
    const described = describe(root);
    const key = `${described.name}@${described.version}`;
    if (packages.has(key)) continue; // the same version installed twice is the same files
    packages.set(key, described);
    for (const file of described.files) {
      const target = join(out, key, file);
      mkdirSync(dirname(target), { recursive: true });
      cpSync(join(root, file), target);
    }
  }
  return [...packages.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, described]) => described);
}

export function licenses(out = join(HERE, 'dist-licenses')) {
  return {
    name: 'scholia-licenses',
    apply: 'build',
    generateBundle(_, bundle) {
      const roots = new Set(IN_STYLES.map((name) => join(HERE, 'node_modules', name)));
      for (const chunk of Object.values(bundle)) {
        for (const id of Object.keys(chunk.modules ?? {})) {
          const found = packageRoot(id);
          if (found) roots.add(found.root);
        }
      }
      rmSync(out, { recursive: true, force: true });
      mkdirSync(out, { recursive: true });
      const packages = record(roots, out);
      writeFileSync(join(out, 'packages.json'), JSON.stringify(packages, null, 1) + '\n');
      if (!existsSync(join(out, 'packages.json'))) this.error('the license manifest was not written');
    },
  };
}
