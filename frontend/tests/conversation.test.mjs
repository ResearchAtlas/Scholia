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
