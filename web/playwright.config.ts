// Playwright (07-testing.md sections 1, 3 and 8): Chromium only, the production build served by the hub test
// server under the hub CSP, browser coverage merged with Vitest in the global teardown (gate 100 %), and the volume
// gate (section 3; ADR-086 §4) with its run-time completeness check in the same teardown. No project depends on
// another: Playwright runs dependent projects in phases one after the other; the projects that must not share a hub
// run at the same time in lanes of their own instead (`e2e/fixtures/env.ts`, decisions/HOT-21.md).
import { cpus } from 'node:os';
import { defineConfig, devices } from '@playwright/test';
import { laneOf, laneOrigin, lanesOf, ORIGIN } from './e2e/fixtures/env.ts';

/** Specs that use the device double (`e2e/helpers/director.ts`) live in `e2e/devices/` (discovered, no list). */
const DEVICE_SPECS = '**/devices/*.spec.ts';

/** The longest device spec (KOR-39: 1 500 stuck tasks, about 330 s) runs in a lane of its own. */
const ATTENTION_VOLUME = '**/devices/attention-volume.spec.ts';
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
/** The volume gate (ADR-086 §4, KOR-35): its own projects, each in a lane of its own (own hub and data set). */
const VOLUME_SPECS = '**/volume/*.spec.ts';
/** The hub of a project's lane. */
const lane = (project: string) => ({ baseURL: laneOrigin(laneOf(project)) });
/**
 * Playwright's default (half the cores Node sees) for the projects of the main lane, which run in parallel, and one
 * more worker for each further lane of the run: a lane's project runs one test at a time and mostly waits for its
 * pages.
 */
const WORKERS = Math.max(1, Math.floor(cpus().length / 2)) + lanesOf(process.argv).length - 1;

export default defineConfig({
  testDir: './e2e',
  testMatch: '*.spec.ts',
  fullyParallel: true,
  workers: WORKERS,
  forbidOnly: true,
  retries: 0,
  reporter: [['list']],
  globalSetup: './e2e/fixtures/global-setup.ts',
  globalTeardown: './e2e/fixtures/global-teardown.ts',
  use: { baseURL: ORIGIN, browserName: 'chromium', headless: true, trace: 'retain-on-failure' },
  projects: [
    // the lanes first: the dispatcher hands out workers in this order, so each lane starts at once
    // one worker each (one signed-in user per project); the data set is seeded once per lane (`volume/data.ts`)
    {
      name: 'volume',
      use: { ...devices['Desktop Chrome'], ...lane('volume') },
      testMatch: VOLUME_SPECS,
      workers: 1,
      timeout: 300_000,
      // the shared helpers (onboarding, join) wait for states with the default 5 s; a large org takes longer
      expect: { timeout: 60_000 },
    },
    {
      name: 'volume-mobile',
      use: { ...devices['Pixel 7'], ...lane('volume-mobile') },
      testMatch: VOLUME_SPECS,
      // live load and scaling run on the desktop only (`PHONE_EXEMPT` of e2e/volume/completeness.ts, ADR-086 §4)
      testIgnore: '**/volume/live.spec.ts',
      workers: 1,
      timeout: 600_000,
      expect: { timeout: 120_000 },
    },
    // the phones' signed-in route sweep of the device specs (06-web.md section 12.7, WEB-13), each phone in a lane of
    // its own
    ...PHONES.map(([name, use]) => ({
      name: `${name}-devices`,
      use: { ...use, ...lane(`${name}-devices`) },
      testMatch: MOBILE_DEVICE_SPECS,
      workers: 1,
    })),
    // specs with the device double run one at a time in their lane: each new device is the first platform candidate
    // of its hub (WEB-04)
    {
      name: 'attention-volume',
      use: { ...devices['Desktop Chrome'], ...lane('attention-volume') },
      testMatch: ATTENTION_VOLUME,
      workers: 1,
    },
    {
      name: 'devices',
      use: { ...devices['Desktop Chrome'], ...lane('devices') },
      testMatch: DEVICE_SPECS,
      testIgnore: ATTENTION_VOLUME,
      workers: 1,
    },
    { name: 'desktop', use: { ...devices['Desktop Chrome'] }, testIgnore: [DEVICE_SPECS, VOLUME_SPECS] },
    // the phones of 06-web.md section 12.7 on Chromium (WEB-13): the mobile specs
    ...PHONES.map(([name, use]) => ({ name, use, testMatch: MOBILE_SPECS, testIgnore: DEVICE_SPECS })),
  ],
});
