// Playwright (07-testing.md sections 1, 3 and 8): Chromium only, the production build served by the hub test
// server under the hub CSP, browser coverage merged with Vitest in the global teardown (gate 100 %).
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
  'community.spec.ts',
].map((name) => `**/e2e/${name}`);
/** Device specs that also run on the phones (the waiting card of KOR-25 and the platform browser must work on a phone). */
const MOBILE_DEVICE_SPECS = [
  '**/devices/responsive.spec.ts',
  '**/devices/task-overview.spec.ts',
  '**/devices/waiting.spec.ts',
  '**/devices/attention.spec.ts',
  '**/devices/pulls.spec.ts',
  '**/devices/browser.spec.ts',
];

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
    { name: 'desktop', use: { ...devices['Desktop Chrome'] }, testIgnore: DEVICE_SPECS },
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
  ],
});
