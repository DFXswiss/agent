// ESLint rule `i18n/no-literal-text` (06-web.md section 2.5): every text a user sees comes from the message catalogs.
// In the UI's JSX it reports text children with a letter and literal strings with a letter in the attributes that
// screen readers or browsers show (`aria-label`, `aria-description`, `title`, `placeholder`, `alt`, `label`).
// Symbols and punctuation (`·`, `–`, `×`, `%`) are layout, not text, and stay allowed.
const LETTER = /\p{L}/u;
const SHOWN = new Set(['aria-label', 'aria-description', 'aria-valuetext', 'title', 'placeholder', 'alt', 'label']);

/** The text of a JSX attribute value that is a plain string (also `{'…'}` and `{`…`}` without expressions). */
function literal(value) {
  if (value === null) return null;
  if (value.type === 'Literal' && typeof value.value === 'string') return value.value;
  if (value.type !== 'JSXExpressionContainer') return null;
  const inner = value.expression;
  if (inner.type === 'Literal' && typeof inner.value === 'string') return inner.value;
  if (inner.type === 'TemplateLiteral' && inner.expressions.length === 0) return inner.quasis[0].value.cooked;
  return null;
}

/** @type {import('eslint').Rule.RuleModule} */
export const noLiteralText = {
  meta: {
    type: 'problem',
    docs: { description: 'UI texts come from the message catalogs (06-web.md section 2.5).' },
    messages: {
      text: 'Hard-coded UI text "{{text}}": use a message key of the catalog (t()).',
      attribute: 'Hard-coded "{{name}}" text "{{text}}": use a message key of the catalog (t()).',
    },
    schema: [],
  },
  create(context) {
    return {
      JSXText(node) {
        const text = node.value.trim();
        if (LETTER.test(text)) context.report({ node, messageId: 'text', data: { text: text.slice(0, 40) } });
      },
      JSXAttribute(node) {
        const name = node.name.type === 'JSXIdentifier' ? node.name.name : null;
        if (name === null || !SHOWN.has(name)) return;
        const text = literal(node.value);
        if (text !== null && LETTER.test(text)) {
          context.report({ node, messageId: 'attribute', data: { name, text: text.slice(0, 40) } });
        }
      },
    };
  },
};

export default { rules: { 'no-literal-text': noLiteralText } };
