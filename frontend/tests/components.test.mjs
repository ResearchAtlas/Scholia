// Components' handlers and render work, checked in their source parsed with ESLint's parser: files
// dropped in the docked Library are imported once, by the Library, and the window's drop overlay
// goes with the drop; the page viewer groups a paper's passages by page once per set of passages;
// a button that starts work is disabled while its request is pending (React applies that before it
// handles the next click, so a double click sends one request); the Library says it is adding while
// any upload is still to finish.
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

// Each byPage call in a source, with the dependencies of the useMemo it is made in ('' when none).
function groupings(text) {
  return parsed(text, (context, found) => ({
    CallExpression(call) {
      if (call.callee.name !== 'byPage') return;
      let memo = '';
      for (let node = call.parent, inner = call; node; inner = node, node = node.parent) {
        if (node.type === 'CallExpression' && node.callee.name === 'useMemo' && node.arguments[0] === inner) {
          memo = context.sourceCode.getText(node.arguments[1]);
          break;
        }
        if (['ArrowFunctionExpression', 'FunctionExpression'].includes(node.type) && node.parent?.callee?.name !== 'useMemo') break;
      }
      (found.calls ??= []).push(memo);
    },
  })).calls ?? [];
}

test('the page viewer groups the passages by page once per set of passages, not for each page it shows', () => {
  assert.deepEqual(groupings(source('Paper.jsx')), ['[passages]']);
});

test('the check finds a grouping made inside the pages\' loop', () => {
  const inLoop = 'function C({ passages, n }) { return Array.from({ length: n }, (_, i) => <P passages={byPage(passages).get(i)} />); }';
  const once = 'function C({ passages, n }) { const pages = useMemo(() => passages && byPage(passages), [passages]); return n; }';
  assert.deepEqual(groupings(inLoop), ['']);
  assert.deepEqual(groupings(once), ['[passages]']);
});

// The disabled attribute of each Button showing one of these labels, with the names the component it
// is in takes from useAction()'s busy (or '' and [] when it has none).
function pendingButtons(text, labels) {
  return parsed(text, (context, found) => {
    const busy = [];
    return {
      VariableDeclarator(node) {
        if (node.init?.callee?.name !== 'useAction' || node.id.type !== 'ObjectPattern') return;
        for (const property of node.id.properties) if (property.key.name === 'busy') busy.push(property.value.name);
      },
      'JSXElement:exit'(element) {
        if (element.openingElement.name.name !== 'Button') return;
        const label = labels.find((key) => context.sourceCode.getText(element).includes(`'${key}'`));
        if (!label) return;
        const disabled = element.openingElement.attributes.find((a) => a.name?.name === 'disabled');
        const names = disabled ? busy.filter((name) => context.sourceCode.getText(disabled).includes(name)) : [];
        (found.buttons ??= {})[label] = names;
      },
    };
  }).buttons ?? {};
}

test('Retry, Cancel, Read again and Replace file are disabled while their request is pending', () => {
  assert.deepEqual(pendingButtons(source('Settings.jsx'), ['runs.retry', 'settings.cancelRun']),
    { 'runs.retry': ['busy'], 'settings.cancelRun': ['busy'] });
  assert.deepEqual(pendingButtons(source('Library.jsx'), ['library.readAgain']), { 'library.readAgain': ['busy'] });
  assert.deepEqual(pendingButtons(source('Paper.jsx'), ['paper.replace']), { 'paper.replace': ['replacing'] });
});

test('the check finds a button its pending request does not disable', () => {
  const button = (disabled) => `function R() { const { busy, run } = useAction();
    return <Button ${disabled} onClick={() => run(retry)}>{t('runs.retry')}</Button>; }`;
  assert.deepEqual(pendingButtons(button(''), ['runs.retry']), { 'runs.retry': [] });
  assert.deepEqual(pendingButtons(button('disabled={busy}'), ['runs.retry']), { 'runs.retry': ['busy'] });
});

// The page image request each effect makes, and whether that effect's cleanup aborts it.
function imageRequests(text) {
  return parsed(text, (context, found) => ({
    CallExpression(call) {
      if (call.callee.name !== 'pageImage') return;
      let effect = call.parent;
      while (effect && !(effect.type === 'ArrowFunctionExpression' && effect.parent?.callee?.name === 'useEffect')) effect = effect.parent;
      const body = effect ? context.sourceCode.getText(effect) : '';
      (found.calls ??= []).push({ signal: call.arguments.length === 4 && context.sourceCode.getText(call.arguments[3]),
        aborted: /return \(\) => \{[^}]*\.abort\(\)/.test(body) });
    },
  })).calls ?? [];
}

test('a page let go while its image loads aborts that request, so no stale render waits for a slot', () => {
  assert.deepEqual(imageRequests(source('Paper.jsx')), [{ signal: 'controller.signal', aborted: true }]);
});

// What the Library's adding is made from, as source text.
function addingFrom(text) {
  return parsed(text, (context, found) => ({
    VariableDeclarator(node) {
      if (/\badding\b/.test(context.sourceCode.getText(node.id))) found.from = context.sourceCode.getText(node.init);
    },
  })).from;
}

test('the Library says it is adding while any upload is still to finish, not only until the first one ends', () => {
  assert.equal(addingFrom(source('Library.jsx')), 'useSyncExternalStore(watchUploads, uploadsWaiting) > 0');
});
