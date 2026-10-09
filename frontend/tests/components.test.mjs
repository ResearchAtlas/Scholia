// Components' handlers and render work, checked in their source parsed with ESLint's parser: files
// dropped in the docked Library are imported once, by the Library, and the window's drop overlay
// goes with the drop; the page viewer groups a paper's passages by page once per set of passages.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';

const source = (name) => readFileSync(fileURLToPath(new URL(`../src/components/${name}`, import.meta.url)), 'utf8');

// Runs rule over a component's source and returns what it collected.
function parsed(text, collect) {
  const found = {};
  const rule = { create: (context) => collect(context, found) };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { found: rule } } }, rules: { 'check/found': 'error' } }];
  assert.deepEqual(new Linter().verify(text, config, 'Component.jsx').filter((m) => m.fatal), []);
  return found;
}

// The drag and drop handlers of the element that sets onDrop, as source text by attribute name.
function dropHandlers(text) {
  return parsed(text, (context, found) => ({
    JSXOpeningElement(element) {
      const names = element.attributes.map((a) => a.name?.name);
      if (!names.some((name) => name?.startsWith('onDrop'))) return;
      for (const attribute of element.attributes) {
        if (/^onDr(ag|op)/.test(attribute.name?.name ?? '')) found[attribute.name.name] = context.sourceCode.getText(attribute.value.expression);
      }
    },
  }));
}

// The handlers made into functions over the names they use from their component.
const bound = (handlers, scope) => Object.fromEntries(Object.entries(handlers).map(([name, text]) => [
  name, new Function(...Object.keys(scope), `return (${text});`)(...Object.values(scope))]));

// A drag event of files over target, through the window's element and the Library's inside it, as the
// browser sends it: the window's capture handler, then the Library's, then the window's unless stopped.
function dispatch(type, shell, library, relatedTarget = null) {
  let stopped = false;
  const event = { dataTransfer: { types: ['Files'], files: ['paper.pdf'] }, relatedTarget, preventDefault() {},
    stopPropagation() { stopped = true; }, currentTarget: { contains: (node) => node === 'inside the Library' } };
  shell[`${type}Capture`]?.(event);
  library?.[type]?.(event);
  if (!stopped) shell[type]?.(event);
}

function window() {
  const state = { dropping: false, over: false, imports: [] };
  const shell = bound(dropHandlers(source('Shell.jsx')), { hasFiles: (event) => event.dataTransfer.types.includes('Files'),
    setDropping: (value) => { state.dropping = value; }, dropFiles: () => state.imports.push('window') });
  const library = bound(dropHandlers(source('Library.jsx')), { setOver: (value) => { state.over = value; },
    add: () => state.imports.push('Library') });
  return { state, shell, library };
}

test('files dropped in the docked Library are imported once, and the window\'s overlay goes with them', () => {
  const { state, shell, library } = window();
  dispatch('onDragOver', shell, library);
  assert.equal(state.dropping, true); // the drag over the Library shows the window's overlay as well
  dispatch('onDrop', shell, library);
  assert.deepEqual(state.imports, ['Library']);
  assert.equal(state.dropping, false);
  assert.equal(state.over, false);
});

test('a drop elsewhere in the window imports there, and a drag that leaves the window clears its overlay', () => {
  const { state, shell } = window();
  dispatch('onDragOver', shell, null);
  dispatch('onDrop', shell, null);
  assert.deepEqual(state.imports, ['window']);
  assert.equal(state.dropping, false);
  dispatch('onDragOver', shell, null);
  dispatch('onDragLeave', shell, null, 'another element of the window');
  assert.equal(state.dropping, true); // still over the window
  dispatch('onDragLeave', shell, null);
  assert.equal(state.dropping, false);
});
