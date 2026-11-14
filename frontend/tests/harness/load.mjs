// The real Library, AddMenu and Paper bodies (and library.js), loaded through Vite's server, the tool the frontend
// already builds and tests with (as tests/memory.test.mjs and tests/hooks.test.mjs draw components), on the stand-in for
// React (standin.mjs): react and its JSX runtimes resolve to the stand-in, the ui components to host elements named for
// them, an icon to nothing, and Parts.jsx to the Paper it would load. No config file is read and nothing is written.
// fresh() gives a new module graph each time: the Library's stores are its module's, so each scenario starts anew.
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createServer } from 'vite';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..', '..');
const src = path.join(root, 'src');
const standin = path.join(here, 'standin.mjs');

// The names the source imports from a module it stubs.
function names(module) {
  const found = new Set();
  for (const entry of readdirSync(src, { recursive: true })) {
    if (!/\.jsx?$/.test(entry)) continue;
    for (const m of readFileSync(path.join(src, entry), 'utf8').matchAll(/import\s*\{([^}]*)\}\s*from\s*'([^']+)'/g)) {
      if (m[2] === module) for (const part of m[1].split(',')) { const name = part.trim().split(/\s+as\s+/)[0]; if (name) found.add(name); }
    }
  }
  return found;
}

const stubs = {
  name: 'library-scenarios-stubs',
  enforce: 'pre',
  resolveId(id) {
    if (/^react(\/jsx-(dev-)?runtime)?$/.test(id)) return standin;
    if (id === 'lucide-react' || id.startsWith('@/components/ui/')) return `\0ui:${id}`;
    if (id === '@/lib/utils') return '\0cn';
    if (id.startsWith('@/')) return path.join(src, id.slice(2));
    if (/\/Parts\.jsx$/.test(id)) return '\0parts';
    return null;
  },
  load(id) {
    if (id === '\0cn') return 'export const cn = (...a) => a.filter((x) => typeof x === "string").join(" ");';
    if (id === '\0parts') return `export { Paper } from ${JSON.stringify(path.join(src, 'components', 'Paper.jsx'))};\n`
      + 'export const ZoteroBrowser = () => null;';
    if (id.startsWith('\0ui:')) {
      const module = id.slice(4);
      return `import { jsx } from ${JSON.stringify(standin)};\n` + [...names(module)].map((name) => (module === 'lucide-react'
        ? `export const ${name} = () => null;` : `export const ${name} = (props) => jsx('ui:${name}', props);`)).join('\n');
    }
    return null;
  },
};

export async function loader() {
  const server = await createServer({
    root, configFile: false, appType: 'custom', logLevel: 'silent', plugins: [stubs],
    esbuild: { jsx: 'automatic' }, ssr: { noExternal: true },
    server: { middlewareMode: true, ws: false, watch: null, hmr: false }, optimizeDeps: { noDiscovery: true, include: [] },
  });
  return {
    async fresh() {
      server.moduleGraph.invalidateAll();
      const [{ Library }, library, { mount, jsx }] = await Promise.all([
        server.ssrLoadModule('/src/components/Library.jsx'), server.ssrLoadModule('/src/library.js'),
        server.ssrLoadModule(standin)]);
      return { Library, library, mount, jsx };
    },
    close: () => server.close(),
  };
}
