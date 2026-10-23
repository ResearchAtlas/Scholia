// A conversation shows the questions of the files attached in it, from its first message on: before
// the window learns of a conversation its first message made (a draft, while that first turn runs or
// after its send failed), files attached there are tagged with the draft's id, and the questions they
// bring are read for that same id. Checked in ConversationView's source, parsed with ESLint's parser:
// the id its questions are read for is the id attach() tags an upload with.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';

const SOURCE = fileURLToPath(new URL('../src/components/ConversationView.jsx', import.meta.url));

// The source text of the id passed to useConversationAsks, and of the conversationId attach() gives addTo.
function ids(source) {
  const found = {};
  const rule = {
    create: (context) => ({
      CallExpression(call) {
        const text = (node) => context.sourceCode.getText(node);
        if (call.callee.name === 'useConversationAsks') found.asks = text(call.arguments[0]);
        if (call.callee.name === 'asksChanged') found.woken = text(call.arguments[0]);
        if (call.callee.name === 'followAsks') found.followed = text(call.arguments[0]);
        if (['afterRead', 'pollsAsks'].includes(call.callee.name)) found[call.callee.name] = true;
        if (call.callee.name === 'afterAsksRead') found.afterRead = true; // S1-19c: afterRead under the library guard
        if (call.callee.name === 'addTo') {
          const visit = (node) => {
            if (node?.type === 'ObjectExpression') {
              for (const property of node.properties) if (property.key?.name === 'conversationId') found.attach = text(property.value);
            } else if (node?.type === 'ConditionalExpression') { visit(node.consequent); visit(node.alternate); }
          };
          visit(call.arguments[4]);
        }
      },
    }),
  };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { ids: rule } } }, rules: { 'check/ids': 'error' } }];
  const messages = new Linter().verify(source, config, 'ConversationView.jsx');
  assert.deepEqual(messages.filter((m) => m.fatal), []);
  return found;
}

test('a draft conversation\'s attached files have their questions read for the draft\'s id', () => {
  const { asks, attach } = ids(readFileSync(SOURCE, 'utf8'));
  assert.ok(attach, 'attach() tags its upload with a conversation');
  assert.equal(asks, attach); // the same id, the draft's while the window does not know the conversation yet
});

test('the check tells the conversation the window knows from the one attach() uses', () => {
  const wired = (asksId) => `function V({ conversation }) { const id = conversation?.id ?? draft;
    const { asks } = useConversationAsks(${asksId}, null);
    async function attach(files) { await addTo(p, files, t, n, id ? { conversationId: id } : {}); } }`;
  assert.deepEqual(ids(wired('id')), { asks: 'id', attach: 'id' });
  assert.notEqual(ids(wired('conversation?.id')).asks, 'id');
});

test('an upload that ends wakes the view showing its conversation, whichever instance that is', () => {
  const { attach, woken, followed } = ids(readFileSync(SOURCE, 'utf8'));
  assert.equal(woken, attach); // attach() says which conversation its files joined, by the id it tagged them with
  assert.equal(followed, 'conversationId'); // and useConversationAsks follows its own conversation's changes
});

test('the view keeps looking after a read that failed, as afterRead and pollsAsks decide', () => {
  const found = ids(readFileSync(SOURCE, 'utf8'));
  assert.ok(found.afterRead && found.pollsAsks); // useConversationAsks keeps its state and polls by them
});

// CT4: the view says which conversation it shows (live.js showing) in a layout effect, which React runs
// in the same commit as the render that changed it, so an open_place arriving after a switch never finds
// the conversation left still shown (a passive effect runs later); its cleanup clears only its own entry.
function shownBy(source) {
  const found = [];
  const rule = {
    create: (context) => ({
      CallExpression(call) {
        if (call.callee.name !== 'showing') return;
        const hook = context.sourceCode.getAncestors(call).reverse().find((node) => node.type === 'CallExpression');
        found.push({ hook: hook?.callee.name, args: call.arguments.map((a) => context.sourceCode.getText(a)).join(', ') });
      },
    }),
  };
  const config = [{ files: ['**/*.jsx'], languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { check: { rules: { shown: rule } } }, rules: { 'check/shown': 'error' } }];
  assert.deepEqual(new Linter().verify(source, config, 'ConversationView.jsx').filter((m) => m.fatal), []);
  return found;
}

test('the conversation shown is said in the commit that shows it (CT4)', () => {
  assert.deepEqual(shownBy(readFileSync(SOURCE, 'utf8')), [{ hook: 'useLayoutEffect', args: 'projectId, id' }]);
  assert.deepEqual(shownBy('function V() { useEffect(() => showing(projectId, id), [projectId, id]); }'),
    [{ hook: 'useEffect', args: 'projectId, id' }]); // the check tells a passive effect apart
});
