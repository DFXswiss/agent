// Playwright (07-testing.md sections 1, 3 and 8): Chromium only, the production build served by the hub test
// server under the hub CSP, browser coverage merged with Vitest in the global teardown (gate 100 %), and the volume
// gate (section 3; ADR-063 §4) with its run-time completeness check in the same teardown.
import { defineConfig, devices } from '@playwright/test';
import { ORIGIN } from './e2e/fixtures/env.ts';

/** Specs that use the device double (`e2e/helpers/director.ts`) live in `e2e/devices/` (discovered, no list). */
const DEVICE_SPECS = '**/devices/*.spec.ts';

/** Phone profiles (Chromium device emulation, 07-testing.md section 1): Pixel 7 and iPhone 15. */
const PHONES: [string, (typeof devices)[string]][] = [
  ['mobile', devices['Pixel 7']],
  ['mobile-iphone', { ...devices['iPhone 15'], defaultBrowserType: 'chromium' }],
];
/** Specs that also run on the phones (outside `e2e/devices/`). */
const MOBILE_SPECS = [
  'notfound.spec.ts',
  'responsive.spec.ts',
  'a11y.spec.ts',
  'security.spec.ts',
  'errors.spec.ts',
  'performance.spec.ts',
].map((name) => `**/e2e/${name}`);
/** Device specs that also run on the phones. */
const MOBILE_DEVICE_SPECS = ['**/devices/responsive.spec.ts', '**/devices/task-overview.spec.ts'];
/** The volume gate (ADR-063 §4, KOR-35): its own projects, after the seed of its data set. */
const VOLUME_SPECS = '**/volume/*.spec.ts';
/**
 * In a full run the volume gate measures after every other project (a quiet machine: the budgets are times); run on
 * its own (`--project volume --project volume-mobile`) it starts with its seed at once.
 */
const SELECTED = process.argv.some((arg) => arg === '--project' || arg.startsWith('--project='));
const OTHERS_LAST = ['desktop', 'mobile', 'mobile-iphone', 'mobile-iphone-devices'];

export default defineConfig({
  testDir: './e2e',
  testMatch: '*.spec.ts',
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  reporter: [['list']],
  globalSetup: './e2e/fixtures/global-setup.ts',
  globalTeardown: './e2e/fixtures/global-teardown.ts',
  use: { baseURL: ORIGIN, browserName: 'chromium', headless: true, trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] }, testIgnore: [DEVICE_SPECS, VOLUME_SPECS] },
    // specs with the device double run one at a time: each new device is the first platform candidate (WEB-04)
    { name: 'devices', use: { ...devices['Desktop Chrome'] }, testMatch: DEVICE_SPECS, workers: 1 },
    // the phones of 06-web.md section 12.7 on Chromium (WEB-13): the mobile specs, and the signed-in route sweep after
    // the device specs (one device spec at a time across all projects)
    ...PHONES.flatMap(([name, use], index) => [
      { name, use, testMatch: MOBILE_SPECS, testIgnore: DEVICE_SPECS },
      {
        name: `${name}-devices`,
        use,
        testMatch: MOBILE_DEVICE_SPECS,
        workers: 1,
        dependencies: [index === 0 ? 'devices' : `${PHONES[index - 1]![0]}-devices`],
      },
    ]),
    // the volume data set, once per run (device double, scripts/hub_test_volume.py)
    { name: 'volume-seed', testMatch: '**/volume/seed.setup.ts', dependencies: SELECTED ? [] : OTHERS_LAST },
    // one worker each (one signed-in user per project), the phone after the desktop (no measurement beside another)
    {
      name: 'volume',
      use: { ...devices['Desktop Chrome'] },
      testMatch: VOLUME_SPECS,
      workers: 1,
      timeout: 180_000,
      dependencies: ['volume-seed'],
    },
    {
      name: 'volume-mobile',
      use: devices['Pixel 7'],
      testMatch: VOLUME_SPECS,
      workers: 1,
      timeout: 300_000,
      dependencies: ['volume'],
    },
  ],
});
