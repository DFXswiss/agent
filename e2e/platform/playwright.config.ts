// Playwright of the platform E2E suite (07-testing.md sections 1, 5.4): Chromium only, started by the orchestrator
// (run.sh) which provides the hub origin, the browser relay and the control API. One worker: the specs share the
// services of a run. Projects (SCN-08): `desktop` runs every spec except the smartphone scenario E2E-29; `mobile`
// (Pixel 7 emulation, touch) runs every web scenario including E2E-29, without the PERF and live specs (they measure
// the devices, not the screen; 07-testing.md sections 2.2 and 6).
import { defineConfig, devices } from '@playwright/test';
import { chromiumArgs, readEnv } from './fixtures/env.ts';

const env = readEnv();
const output = `${env.iterationDir}/playwright`;

export default defineConfig({
  testDir: '.',
  // live/: the @live tests (07-testing.md section 6), selected only by `run.sh --live`.
  testMatch: ['specs/**/*.spec.ts', 'perf/**/*.spec.ts', 'live/**/*.spec.ts'],
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 120_000,
  outputDir: `${output}/test-results`,
  reporter: [['list'], ['json', { outputFile: `${output}/results.json` }]],
  globalTeardown: './fixtures/global-teardown.ts',
  use: {
    baseURL: env.hubOrigin,
    browserName: 'chromium',
    headless: true,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    launchOptions: { args: chromiumArgs(env) },
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] }, testIgnore: ['specs/e2e-29-*.spec.ts'] },
    { name: 'mobile', use: { ...devices['Pixel 7'] }, testIgnore: ['perf/**', 'live/**'] },
  ],
});
