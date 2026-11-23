// After the suite: write the browser coverage collected by the WEB-01 page fixture (raw V8 data and a summary,
// below the iteration's web-coverage directory = the working directory of the run). Informative only: the web gate
// is reached by `make platform-web` (07-testing.md section 3); a failure here never fails the run.
import { CoverageReport } from 'monocart-coverage-reports';
import { e2eCoverage } from '../../../web/e2e/fixtures/coverage-options.ts';

export default async function globalTeardown(): Promise<void> {
  try {
    await new CoverageReport({
      ...e2eCoverage,
      name: 'web coverage of the platform E2E run',
      reports: ['raw', 'console-summary'],
    }).generate();
  } catch (error) {
    console.log(`web coverage report skipped: ${String(error)}`);
  }
}
