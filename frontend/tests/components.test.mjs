// Components' handlers and render work, checked in their source parsed with ESLint's parser: files
// dropped in the docked Library are imported once, by the Library, and the window's drop overlay
// goes with the drop; each read of a paper's text made for a part near the view (a page's image,
// its passages, a stretch of passages) goes when the part is let go; each passage of the text says
// where it is in the whole; a button that starts work is disabled while its request is pending (React applies that before it
// handles the next click, so a double click sends one request); the Library says it is adding while
// any upload is still to finish; a PDF page keeps what it shows out of reach while another part of
// its passages loads, and asks again for a part whose read failed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';
import { waitsOn } from '../src/library.js';

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

// The reads of a paper's text each effect makes, and whether that effect's cleanup aborts them.
const READS = ['pageImage', 'pagePart', 'passageStretch'];
function textReads(text) {
  return parsed(text, (context, found) => ({
    CallExpression(call) {
      if (!READS.includes(call.callee.name)) return;
      let effect = call.parent;
      while (effect && !(effect.type === 'ArrowFunctionExpression' && effect.parent?.callee?.name === 'useEffect')) effect = effect.parent;
      const body = effect ? context.sourceCode.getText(effect) : '';
      (found.calls ??= []).push({ read: call.callee.name, signal: context.sourceCode.getText(call.arguments.at(-1)),
        aborted: /return \(\) => \{[^}]*\.abort\(\)/.test(body) });
    },
  })).calls ?? [];
}

test('a page or a stretch of passages let go while it loads aborts its requests, so no stale read waits for a slot', () => {
  assert.deepEqual(textReads(source('Paper.jsx')), READS.map((read) => ({ read, signal: 'controller.signal', aborted: true })));
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

// The aria-posinset and aria-setsize of each element whose role is listitem, as source text.
function positions(text) {
  return parsed(text, (context, found) => ({
    JSXOpeningElement(element) {
      const attribute = (name) => element.attributes.find((a) => a.name?.name === name);
      if (attribute('role')?.value?.value !== 'listitem') return;
      (found.items ??= []).push(['aria-posinset', 'aria-setsize'].map((name) => (attribute(name)
        ? context.sourceCode.getText(attribute(name).value.expression) : null)));
    },
  })).items ?? [];
}

test('each passage of the text says where it is in the whole text, as only the stretches held are in the page', () => {
  assert.deepEqual(positions(source('Paper.jsx')), [['passage.ordinal + 1', 'count']]);
  assert.deepEqual(positions('const C = () => <div role="listitem" />;'), [[null, null]]);
});

// A PDF page's move to another part of its passages, as source text: the inert of the element
// around each passage and each button to another part, the onClick of each button such a button
// comes with, the page's keydown handler, go, the effect reading a part and what it depends on, the
// effect giving focus to Retry, and passTo, through which it does.
function partMoves(text) {
  return parsed(text, (context, found) => {
    const code = (node) => context.sourceCode.getText(node);
    const inertOf = (node) => {
      for (let around = node.parent; around; around = around.parent) {
        const inert = around.type === 'JSXElement' && around.openingElement.attributes.find((a) => a.name?.name === 'inert');
        if (inert) return code(inert.value.expression);
      }
      return null;
    };
    return {
      JSXOpeningElement(element) {
        const attribute = (name) => element.attributes.find((a) => a.name?.name === name);
        if (attribute('data-passage')) (found.passages ??= []).push(inertOf(element.parent));
        if (element.name.name === 'figure' && attribute('onKeyDown')) found.keyDown = code(attribute('onKeyDown').value.expression);
        let control = element.parent;
        while (control && !(control.type === 'VariableDeclarator' && control.id.name === 'control')) control = control.parent;
        if (control && element.name.name === 'button') (found.asks ??= []).push(code(attribute('onClick').value.expression));
      },
      CallExpression(call) {
        if (call.callee.name === 'control') (found.controls ??= []).push(inertOf(call));
        if (call.callee.name === 'useEffect' && code(call.arguments[0]).includes('retry')) found.toRetry = code(call.arguments[0]);
        if (call.callee.name !== 'pagePart') return;
        let effect = call.parent;
        while (effect && effect.callee?.name !== 'useEffect') effect = effect.parent;
        found.read = code(effect.arguments[0]);
        found.reads = effect.arguments[1].elements.map((name) => name.name);
      },
      VariableDeclarator(node) {
        if (node.id.name === 'go') found.go = code(node.init);
        if (node.id.name === 'passTo') found.passTo = code(node.init);
      },
    };
  });
}

test('a PDF page keeps what it shows out of reach while another part loads, and its button or Retry asks again for a part whose read failed', () => {
  const found = partMoves(source('Paper.jsx'));
  assert.deepEqual(found.passages, ['move.loading', null]); // a page's passages; the text view never shows a part in another's place
  assert.deepEqual(found.controls, ['move.loading', 'move.loading']);
  // While the part loads, Tab toward it waits on the page for passOn (Tab to the part after, Shift+Tab
  // to the part before), and Tab the other way leaves the page.
  const held = (loading, part, shiftKey) => {
    let prevented = false;
    new Function('move', 'part', 'shown', `return (${found.keyDown});`)({ loading }, part, { part: 1 })(
      { key: 'Tab', shiftKey, preventDefault: () => { prevented = true; } });
    return prevented;
  };
  assert.deepEqual([held(true, 2, false), held(true, 2, true), held(true, 0, true), held(true, 0, false)], [true, false, true, false]);
  assert.deepEqual([held(false, 1, false), held(false, 1, true)], [false, false]);
  // Retry asks as the button does, and each ask clears a failed read, on which the read depends (partMove's read).
  assert.deepEqual(found.asks, ['() => go(to, where)', '() => go(to, where)']);
  const calls = [];
  const go = new Function('focusOn', 'setPart', 'setFailed', `return (${found.go});`)(
    (where) => calls.push(['focusOn', where]), (part) => calls.push(['setPart', part]), (failed) => calls.push(['setFailed', failed]));
  go(1, 'first');
  assert.deepEqual(calls, [['focusOn', 'first'], ['setPart', 1], ['setFailed', false]]);
  assert.ok(['shown', 'part', 'failed'].every((name) => found.reads.includes(name)), found.reads);
  // Let go, a page comes back on the part it showed, its failure cleared, not on one still to come or failed.
  const letGo = (shown, part) => {
    const set = [];
    new Function('held', 'shown', 'part', 'setPart', 'setShown', 'setFailed', `return (${found.read});`)(false, shown, part,
      (value) => set.push(['part', value]), (value) => set.push(['shown', value]), (value) => set.push(['failed', value]))();
    return set;
  };
  assert.deepEqual(letGo({ part: 0 }, 1), [['part', 0], ['shown', null], ['failed', false]]);
  assert.deepEqual(letGo(null, 1), [['part', 1], ['shown', null], ['failed', false]]);
  // A failed read gives focus to Retry only through passTo, so only while focus waits on the page (waitsOn).
  const passed = [];
  const toRetry = (failed) => new Function('move', 'passTo', 'retry', 'document', 'frame', `return (${found.toRetry});`)(
    { failed }, (element) => passed.push(element), { current: 'Retry' }, { activeElement: 'page' }, { current: 'page' })();
  toRetry(false);
  toRetry(true);
  assert.deepEqual(passed, ['Retry']);
  const focused = (active, entering) => {
    let got = false;
    new Function('waitsOn', 'frame', 'document', 'entering', `return (${found.passTo});`)(waitsOn, { current: 'page' },
      { activeElement: active }, { current: entering })({ focus: () => { got = true; } });
    return got;
  };
  assert.deepEqual([focused('page', 'first'), focused('page', null), focused('elsewhere', 'first')], [true, false, false]);
});
