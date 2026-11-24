// ESLint flat configuration. Rules follow the DFX `app` repository (typescript-eslint recommended, unused names
// allowed only with a leading underscore) plus the React hooks rules; formatting is Prettier's job.
import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import i18n from './eslint/i18n.js';

export default tseslint.config(
  { ignores: ['node_modules/', 'test-results/', 'playwright-report/', 'e2e/.probe/'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}', 'tests/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      'no-restricted-syntax': [
        'error',
        {
          selector: "MemberExpression[object.type='MetaProperty'][property.name='env'] > Identifier[name='MODE']",
          message: 'No test-only code paths (06-web.md section 13).',
        },
      ],
    },
  },
  {
    // UI texts only from the message catalogs (06-web.md section 2.5, WEB-13).
    files: ['src/ui/**/*.tsx'],
    plugins: { i18n },
    rules: { 'i18n/no-literal-text': 'error' },
  },
  {
    // src/core is framework-free (06-web.md section 2.2).
    files: ['src/core/**/*.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            { group: ['react', 'react-*', 'react/*', '**/ui/**'], message: 'src/core must stay framework-free.' },
          ],
        },
      ],
    },
  },
  prettier,
);
