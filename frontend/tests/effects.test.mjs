// Effects survive React's StrictMode, which runs each effect's setup, cleanup and setup again: a
// cleanup that sets a ref's flag (shown.current = false) must have its setup set it as well, or
// the flag stays as the cleanup left it while the component is still shown. And a state update
// passed as a function never reads a ref: React may run it after the call, when the ref holds
// something newer. Parsed with ESLint's parser, over every component and module.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { basename, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));
const isFunction = (node) => ['ArrowFunctionExpression', 'FunctionExpression'].includes(node?.type);

// Calls visit(node) on node and everything under it; into nested functions only when deep.
function walk(node, visit, deep) {
  if (!node || typeof node.type !== 'string') return;
  visit(node);
  for (const [key, value] of Object.entries(node)) {
    if (key === 'parent') continue;
    for (const child of Array.isArray(value) ? value : [value]) {
      if (child && typeof child.type === 'string' && (deep || !isFunction(child))) walk(child, visit, deep);
    }
  }
}

// The ref a `name.current = value` assignment sets, with its value, or null.
function refFlag(node) {
  return node.type === 'AssignmentExpression' && node.operator === '=' && node.left.type === 'MemberExpression'
    && node.left.property.name === 'current' && node.left.object.type === 'Identifier'
    ? { ref: node.left.object.name, value: node.right } : null;
}

const cleanupRestored = {
  create: (context) => ({
    CallExpression(call) {
      const effect = call.arguments[0];
      if (call.callee.name !== 'useEffect' || !isFunction(effect)) return;
      const cleanups = [];
      const setUp = new Set();
      if (isFunction(effect.body)) cleanups.push(effect.body); // useEffect(() => () => ...): no setup at all
      else {
        walk(effect.body, (node) => {
          if (node.type === 'ReturnStatement' && isFunction(node.argument)) cleanups.push(node.argument);
          const flag = refFlag(node);
          if (flag) setUp.add(flag.ref);
        }, false);
      }
      for (const cleanup of cleanups) {
        walk(cleanup.body, (node) => {
          const flag = refFlag(node);
          if (flag && flag.value.type === 'Literal' && typeof flag.value.value === 'boolean' && !setUp.has(flag.ref)) {
            context.report({ node, message: `${flag.ref}.current is set by the cleanup but not by the setup` });
          }
        }, true);
      }
    },
  }),
};

// The setters of useState in a file, and every function passed to one: none reads a ref's current.
const updaterReadsNoRef = {
  create: (context) => {
    const setters = new Set();
    return {
      VariableDeclarator(node) {
        if (node.id.type === 'ArrayPattern' && node.init?.type === 'CallExpression' && node.init.callee.name === 'useState'
          && node.id.elements[1]?.type === 'Identifier') setters.add(node.id.elements[1].name);
      },
      CallExpression(call) {
        const update = call.arguments[0];
        if (!setters.has(call.callee.name) || !isFunction(update)) return;
        walk(update.body, (node) => {
          if (node.type === 'MemberExpression' && !node.computed && node.property.name === 'current') {
            context.report({ node, message: `the update passed to ${call.callee.name} reads a ref's current` });
          }
        }, true);
      },
    };
  },
};

const CONFIG = [{
  files: ['**/*.js', '**/*.jsx'],
  languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
  plugins: { effects: { rules: { 'cleanup-restored': cleanupRestored, 'updater-reads-no-ref': updaterReadsNoRef } } },
  rules: { 'effects/cleanup-restored': 'error', 'effects/updater-reads-no-ref': 'error' },
}];

function problems(source, file = 'sample.jsx') {
  return new Linter().verify(source, CONFIG, file).map((m) => `${basename(file)}:${m.line} ${m.message}`);
}

test('no effect leaves a flag as its cleanup set it, under StrictMode, and no update reads a ref', () => {
  const files = readdirSync(SRC, { recursive: true }).filter((name) => /\.jsx?$/.test(name)).map((name) => join(SRC, name));
  assert.deepEqual(files.flatMap((file) => problems(readFileSync(file, 'utf8'), file)), []);
});

test('the check finds a cleanup-only flag and accepts one its setup sets', () => {
  const bad = 'function A() { const shown = useRef(true);\n useEffect(() => () => { shown.current = false; }, []); }';
  assert.deepEqual(problems(bad), ['sample.jsx:2 shown.current is set by the cleanup but not by the setup']);
  const good = 'function A() { const shown = useRef(true);\n'
    + ' useEffect(() => { shown.current = true; return () => { shown.current = false; }; }, []); }';
  assert.deepEqual(problems(good), []);
  const counter = 'function A() { useEffect(() => { load(); return () => { latest.current += 1; }; }, []); }';
  assert.deepEqual(problems(counter), []); // a counter moved on by the cleanup is no flag
});

test('the check finds an update that reads a ref and accepts one given the value read before it', () => {
  const bad = 'function A() { const [form, setForm] = useState({}); const shown = useRef({});\n'
    + ' useEffect(() => { setForm((current) => merge(shown.current, current)); shown.current = next; }, [key]); }';
  assert.deepEqual(problems(bad), ["sample.jsx:2 the update passed to setForm reads a ref's current"]);
  const good = 'function A() { const [form, setForm] = useState({}); const shown = useRef({});\n'
    + ' useEffect(() => { const before = shown.current; shown.current = next;\n'
    + ' setForm((current) => merge(before, current)); }, [key]); }';
  assert.deepEqual(problems(good), []);
  const timer = 'function A() { const latest = useRef(0); setTimeout(() => latest.current, 10); }';
  assert.deepEqual(problems(timer), []); // only the state setters of useState
});
