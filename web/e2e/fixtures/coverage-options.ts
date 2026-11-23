// Coverage options shared by Vitest (vitest-monocart-coverage) and Playwright (monocart-coverage-reports): both
// write raw V8 data that is mapped onto the TypeScript sources of `src/` and merged into one gate (07-testing.md
// section 3: `src/**` 100 % lines and branches, Vitest + Playwright merged).
import type { CoverageReportOptions } from 'monocart-coverage-reports';

// Inside node_modules/: generated, git-ignored and outside the repository hygiene scan (raw data holds absolute paths).
export const COVERAGE_DIR = 'node_modules/.coverage';
export const VITEST_RAW = `${COVERAGE_DIR}/vitest/raw`;
export const E2E_RAW = `${COVERAGE_DIR}/e2e/raw`;
export const REQUIRED_PCT = 100;

/** Any spelling of an app source (`src/x.ts`, `web/src/x.ts`, `../web/src/x.ts`, `host/assets/src/x.ts`) → `src/x.ts`. */
export function sourcePath(filePath: string): string {
  const match = /(?:^|\/)(src\/.*)$/.exec(filePath.replace(/\\/g, '/'));
  return match ? match[1]! : filePath;
}

export const isAppSource = (path: string): boolean => /^src\/.+\.tsx?$/.test(path) && !path.endsWith('.d.ts');

const common = {
  sourcePath,
  sourceFilter: (path: string) => isAppSource(sourcePath(path)),
} satisfies CoverageReportOptions;

/** The Vitest data of the core alone: which core files Vitest loaded (their gate is Vitest's own thresholds). */
export const coreCoverage: CoverageReportOptions = {
  ...common,
  name: 'web core coverage (Vitest)',
  inputDir: [VITEST_RAW],
  outputDir: `${COVERAGE_DIR}/core`,
  reports: [],
  sourceFilter: (path: string) => isAppSource(sourcePath(path)) && !isBrowserGated(sourcePath(path)),
};

export const vitestCoverage: CoverageReportOptions = {
  ...common,
  name: 'web unit coverage (Vitest)',
  outputDir: `${COVERAGE_DIR}/vitest`,
  reports: ['raw', 'console-summary'],
};

/**
 * `src/core/**` is gated by Vitest alone (07-testing.md section 3: thresholds of `vitest.config.ts`) and stays out of
 * the merge: the bundler reprints the code it executes (e.g. drops the braces of single-statement `if` blocks), so the
 * browser's branch ranges of core files do not line up with Vitest's and would count as extra, uncovered branches
 * (decision WEB-03). The merge covers the rest of src/.
 */
export const isBrowserGated = (path: string): boolean => isAppSource(path) && !path.startsWith('src/core/');

export const e2eCoverage: CoverageReportOptions = {
  ...common,
  name: 'web browser coverage (Playwright)',
  outputDir: `${COVERAGE_DIR}/e2e`,
  reports: ['raw'],
  // the bundler's own runtime chunk has no app source
  entryFilter: (entry) => /^\/assets\/(?!rolldown-runtime-)[^/]+\.js$/.test(new URL(entry.url).pathname),
};

export const mergedCoverage: CoverageReportOptions = {
  ...common,
  sourceFilter: (path: string) => isBrowserGated(sourcePath(path)),
  name: 'web merged coverage (Vitest + Playwright)',
  inputDir: [VITEST_RAW, E2E_RAW],
  outputDir: `${COVERAGE_DIR}/merged`,
  reports: ['console-details', 'json-summary', 'lcovonly', 'v8'],
};
