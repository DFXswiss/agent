// After the Playwright suite: write the browser coverage, merge it with the Vitest data and fail below 100 %
// lines or branches over all of src/ (07-testing.md section 3).
import { CoverageReport } from 'monocart-coverage-reports';
import { e2eCoverage, REQUIRED_PCT } from './coverage-options.ts';
import { mergedGate } from './coverage.ts';
import { WEB_DIR } from './env.ts';

export default async function globalTeardown() {
  process.chdir(WEB_DIR);
  await new CoverageReport(e2eCoverage).generate();
  const { lines, branches, missing, passed } = await mergedGate();
  console.log(`merged web coverage: lines ${lines} %, branches ${branches} % (required ${REQUIRED_PCT} %)`);
  if (missing.length) console.log(`sources without any coverage data: ${missing.join(', ')}`);
  if (!passed) throw new Error(`merged web coverage below ${REQUIRED_PCT} %`);
}
