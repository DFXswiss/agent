// After the Playwright suite: write the browser coverage, merge it with the Vitest data and fail below 100 %
// lines or branches over all of src/ (07-testing.md section 3); when the volume data set was seeded in this run, fail
// unless every id a volume spec covers was measured in both volume projects (ADR-086 §4, KOR-35).
import { CoverageReport } from 'monocart-coverage-reports';
import { runtimeGaps, unseedable } from '../volume/completeness.ts';
import { UNSEEDABLE } from '../volume/gaps.ts';
import { e2eCoverage, REQUIRED_PCT } from './coverage-options.ts';
import { mergedGate } from './coverage.ts';
import { WEB_DIR } from './env.ts';

export default async function globalTeardown() {
  process.chdir(WEB_DIR);
  const unmeasured = runtimeGaps(WEB_DIR, UNSEEDABLE);
  for (const [id, reason] of Object.entries(unseedable(WEB_DIR)))
    console.log(`volume gate: known gap ${id} – the data set cannot contain it: ${reason}`);
  if (unmeasured.length) console.log(`volume gate: covered but not measured:\n  ${unmeasured.join('\n  ')}`);
  await new CoverageReport(e2eCoverage).generate();
  const { lines, branches, missing, passed } = await mergedGate();
  console.log(`merged web coverage: lines ${lines} %, branches ${branches} % (required ${REQUIRED_PCT} %)`);
  if (missing.length) console.log(`sources without any coverage data: ${missing.join(', ')}`);
  if (unmeasured.length) throw new Error(`volume gate: ${unmeasured.length} covered ids not measured in both projects`);
  if (!passed) throw new Error(`merged web coverage below ${REQUIRED_PCT} %`);
}
