// A Vite plugin that records the npm packages a build bundles, for the license audit
// (tools/license_audit.py). It writes dist-licenses/packages.json, each package's name,
// version, license and license files, and copies those files beside it. Only modules the
// bundle contains count, so development tools never appear, apart from the packages whose
// code the stylesheet copies: Tailwind's base styles and tailwindcss-animate's keyframes.
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
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
  return { name: manifest.name, version: manifest.version, license, files };
}

export function licenses(out = join(HERE, 'dist-licenses')) {
  return {
    name: 'scholia-licenses',
    apply: 'build',
    generateBundle(_, bundle) {
      const roots = new Map(IN_STYLES.map((name) => [name, join(HERE, 'node_modules', name)]));
      for (const chunk of Object.values(bundle)) {
        for (const id of Object.keys(chunk.modules ?? {})) {
          const found = packageRoot(id);
          if (found && !roots.has(found.name)) roots.set(found.name, found.root);
        }
      }
      rmSync(out, { recursive: true, force: true });
      mkdirSync(out, { recursive: true });
      const packages = [...roots.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, root]) => {
        const described = describe(root);
        for (const file of described.files) {
          const target = join(out, name, file);
          mkdirSync(dirname(target), { recursive: true });
          cpSync(join(root, file), target);
        }
        return described;
      });
      writeFileSync(join(out, 'packages.json'), JSON.stringify(packages, null, 1) + '\n');
      if (!existsSync(join(out, 'packages.json'))) this.error('the license manifest was not written');
    },
  };
}
