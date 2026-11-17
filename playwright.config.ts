import { defineConfig, devices } from '@playwright/test';
import { EMPTY_URL, FILLED_URL } from './e2e/servers';

// Visual regression baselines of the local dashboard. A local review aid, not
// run in CI; see CONTRIBUTING.md, "Visual regression tests (Playwright)".

// The Python that runs the dashboard: the repository's virtual environment with
// `pip install -e ".[test]"` (the seed reuses the fixtures under tests/).
const python = process.env.AGENT_E2E_PYTHON || '.venv/bin/python';

function dashboard(url: string, data: 'filled' | 'empty') {
  return {
    command: `${python} e2e/dashboard_server.py --port ${new URL(url).port} --data ${data}`,
    url: `${url}/`,
    // Never reuse a server: a dashboard already listening there is not the seeded one.
    reuseExistingServer: false,
    timeout: 120000,
    stdout: 'ignore' as const,
    stderr: 'pipe' as const,
    // SIGTERM lets the script stop its PostgreSQL cluster and remove its home.
    gracefulShutdown: { signal: 'SIGTERM' as const, timeout: 15000 },
  };
}

export default defineConfig({
  testDir: './e2e',
  snapshotDir: './e2e/screenshots/baseline',
  snapshotPathTemplate: '{snapshotDir}/{testFileName}-{arg}-{projectName}-{platform}{ext}',
  outputDir: './e2e/test-results',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  timeout: 60000,
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    timezoneId: 'UTC',
    locale: 'en-US',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: [dashboard(FILLED_URL, 'filled'), dashboard(EMPTY_URL, 'empty')],
});
