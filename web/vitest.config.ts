// Vitest (07-testing.md sections 1 and 3): `tests/core` and `tests/build` run in Node, `tests/ui` in jsdom.
// Coverage is collected by @vitest/coverage-v8 through vitest-monocart-coverage: Vitest's own thresholds enforce
// 100 % for src/core alone, and the raw V8 data of all of src/ is kept for the merged gate of `npm run test:e2e`.
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';
import { vitestCoverage } from './e2e/fixtures/coverage-options.ts';

const FULL = { lines: 100, branches: 100, functions: 100, statements: 100 };

export default defineConfig({
  plugins: [react()],
  // The production JSX transform, as in the build: the browser's coverage ranges of JSX branches then line up with
  // Vitest's (the development runtime adds arguments, so its ranges end elsewhere; decision WEB-03).
  oxc: { jsx: { development: false } },
  test: {
    restoreMocks: true,
    projects: [
      {
        extends: true,
        test: { name: 'node', environment: 'node', include: ['tests/{core,build,scripts}/**/*.test.ts'] },
      },
      {
        extends: true,
        test: {
          name: 'ui',
          environment: 'jsdom',
          include: ['tests/ui/**/*.test.{ts,tsx}'],
          setupFiles: ['tests/ui/setup.ts'],
        },
      },
    ],
    coverage: {
      provider: 'custom',
      customProviderModule: 'vitest-monocart-coverage',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.d.ts'],
      thresholds: { 'src/core/**': FULL },
      // @ts-expect-error option of vitest-monocart-coverage, passed through to monocart-coverage-reports
      coverageReportOptions: {
        ...vitestCoverage,
        // a run that ended before its report (killed, interrupted) leaves its cache behind, and the next run would
        // merge those stale sources in: an older version of a changed file then counts as a file of its own in the
        // merged gate, with the coverage of that old version (KOR-35)
        cleanCache: true,
      },
    },
  },
});
