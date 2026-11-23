// The lint rule against hard-coded UI texts (06-web.md section 2.5, WEB-13): JSX text and shown attributes with a
// letter are reported, catalog lookups, symbols and other attributes are not; the rule is active for src/ui.
import { ESLint, RuleTester } from 'eslint';
import { describe, expect, it } from 'vitest';
import { noLiteralText } from '../../eslint/i18n.js';
import { WEB_DIR } from '../helpers';

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.itOnly = it.only;

const tester = new RuleTester({ languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } } });

tester.run('i18n/no-literal-text', noLiteralText, {
  valid: [
    '<p>{t("home.title")}</p>',
    '<span aria-hidden="true">·</span>',
    '<span>{count} – {total} %</span>',
    '<input aria-label={t("x")} placeholder={label} type="text" className="block" />',
    '<a href="/security" data-testid="shell.footer.security" title={`${name}`}>{t("x")}</a>',
    '<img alt="" src="/a.svg" />',
    '<p {...props}>{t("x")}</p>',
    '<a xlink:title="Name" />',
  ],
  invalid: [
    { code: '<p>Hello</p>', errors: [{ messageId: 'text' }] },
    { code: '<p>  Grüezi  {name}</p>', errors: [{ messageId: 'text' }] },
    { code: '<button aria-label="Close">×</button>', errors: [{ messageId: 'attribute' }] },
    { code: '<input placeholder={"Search"} />', errors: [{ messageId: 'attribute' }] },
    { code: '<img alt={`Logo`} />', errors: [{ messageId: 'attribute' }] },
    { code: '<option label="Deutsch" />', errors: [{ messageId: 'attribute' }] },
  ],
});

describe('lint configuration', () => {
  it('applies the rule to the UI only', async () => {
    const eslint = new ESLint({ cwd: WEB_DIR });
    const [ui] = await eslint.lintText('export const A = () => <p>Hello</p>;\n', { filePath: 'src/ui/x.tsx' });
    expect(ui!.messages.map((message) => message.ruleId)).toContain('i18n/no-literal-text');
    const [test] = await eslint.lintText('export const A = () => <p>Hello</p>;\n', { filePath: 'tests/ui/x.tsx' });
    expect(test!.messages.map((message) => message.ruleId)).not.toContain('i18n/no-literal-text');
    // loading the typed lint configuration takes seconds on a busy machine
  }, 60_000);
});
