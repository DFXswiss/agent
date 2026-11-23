// Browser V8 coverage of the production bundle, mapped through the hidden source maps of the build onto src/.
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { CoverageReport, type CoverageReportOptions } from 'monocart-coverage-reports';
import { coreCoverage, E2E_RAW, isAppSource, mergedCoverage, REQUIRED_PCT, VITEST_RAW } from './coverage-options.ts';
import { STATIC_DIR, WEB_DIR } from './env.ts';

interface ScriptEntry {
  url: string;
  source?: string;
  sourceMap?: unknown;
}

/** Attaches the hidden source map of every bundle file (the delivered code carries no reference to it). */
export function withSourceMaps<T extends ScriptEntry>(entries: T[], staticDir: string = STATIC_DIR): T[] {
  return entries.map((entry) => {
    const path = new URL(entry.url).pathname;
    const map = join(staticDir, `${path}.map`);
    return path.startsWith('/assets/') && existsSync(map)
      ? { ...entry, sourceMap: JSON.parse(readFileSync(map, 'utf8')) }
      : entry;
  });
}

export interface GateResult {
  readonly lines: number;
  readonly branches: number;
  /** Source files of src/ that neither Vitest nor Playwright loaded (they would escape the gate). */
  readonly missing: string[];
  readonly passed: boolean;
}

/** Every TypeScript source of src/ (POSIX paths relative to web/). */
export function appSources(webDir: string = WEB_DIR): string[] {
  return readdirSync(join(webDir, 'src'), { recursive: true, encoding: 'utf8' })
    .map((path) => `src/${path.split('\\').join('/')}`)
    .filter(isAppSource)
    .sort();
}

/**
 * Merges the raw Vitest and Playwright data and enforces 100 % lines and branches over src/ outside the core (the core
 * is gated by Vitest's own thresholds); every source of src/ must have coverage data from one of the two.
 */
export async function mergedGate(
  options: CoverageReportOptions = mergedCoverage,
  core: CoverageReportOptions = coreCoverage,
): Promise<GateResult> {
  if (!existsSync(join(WEB_DIR, VITEST_RAW))) throw new Error(`${VITEST_RAW} missing: run npm run test:coverage first`);
  if (!existsSync(join(WEB_DIR, E2E_RAW))) throw new Error(`${E2E_RAW} missing: no browser coverage was collected`);
  const results = await new CoverageReport(options).generate();
  const summary = results!.summary;
  // Exact ratios from the counts (the reported percentages are rounded).
  const pct = ({ covered, total }: { covered: number; total: number }) => (total ? (100 * covered) / total : 100);
  const lines = pct(summary.lines);
  const branches = pct(summary.branches);
  const coreFiles = (await new CoverageReport(core).generate())!.files;
  const reported = new Set([...results!.files, ...coreFiles].map((file) => file.sourcePath));
  const missing = appSources().filter((path) => !reported.has(path));
  return { lines, branches, missing, passed: lines >= REQUIRED_PCT && branches >= REQUIRED_PCT && !missing.length };
}
