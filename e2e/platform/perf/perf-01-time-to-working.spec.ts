// PERF-01 (07-testing.md sections 2.2 and 5.3, R-3.10, R-14.3): time to working on a warm pool. Before every run the
// pool is filled to its configured size; each run is the first task of a new org (series "new org") or of a new
// workgroup in an existing org (series "new workgroup"). Pass: every run and the p95 below 10 s. The final acceptance
// numbers (20 + 10 runs) are taken by ACC-M1/PFM-01 with E2E_PERF_RUNS/E2E_PERF_SHORT_RUNS; the default is 5 runs per
// series. Every run's start estimate is the warm-pool value (AUD-01 A-30, PFM-01).
import { expect, test } from '../fixtures/test.ts';
import { onboard, platformDevice, poolReady, poolSize, sendPrompt } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { evaluate, firstTaskInNewOrg, POOL_ESTIMATE_MS, PROMPT, RUNS, scenario, SHORT_RUNS } from './measure.ts';
import { navTo } from '../fixtures/nav.ts';

test.describe.configure({ mode: 'serial' });

test('@PERF-01 @R-3.10 @R-14.3 @needs-vm warm pool: the first task of a new org works in under 10 s', async ({
  browser,
  orchestrator,
  e2e,
}) => {
  test.setTimeout(RUNS * 120_000 + 300_000);
  await orchestrator.loadScenario('model', scenario(RUNS));
  await platformDevice(orchestrator);
  const size = await poolSize(orchestrator);
  const tasks: string[] = [];
  for (let run = 0; run < RUNS; run += 1) {
    await poolReady(orchestrator, size);
    tasks.push((await firstTaskInNewOrg(browser, e2e.hubOrigin, orchestrator, `Perf org ${run + 1}`)).task);
  }
  const series = await evaluate(orchestrator, 'PERF-01 new org', tasks);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 120_000 }).toBe(true);
  expect(series.summary.runs).toBe(RUNS);
  expect(series.summary.passed, JSON.stringify(series.runs)).toBe(true);
  // AUD-01 A-30: the hub's estimate follows the pool it knows (filled: 3000 ms, 03-api.md section 4.6.2).
  expect(series.runs.map((run) => run.estimate_ms)).toEqual(tasks.map(() => POOL_ESTIMATE_MS));
});

test('@PERF-01 @R-3.10 @needs-vm warm pool: the first task of a new workgroup in an existing org works in under 10 s', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(SHORT_RUNS * 120_000 + 300_000);
  await orchestrator.loadScenario('model', scenario(SHORT_RUNS));
  await platformDevice(orchestrator);
  const size = await poolSize(orchestrator);
  await authenticator('platform');
  const { org, recoveryCode } = await onboard(page, orchestrator, 'Perf workgroups');
  const user = await PageUser.open(page, recoveryCode);
  // All workgroups are created before the first one gets the platform device (the test helper builds log batches
  // without the platform state, which an org with a platform device needs); each is new when its run starts.
  const workgroups: string[] = [];
  for (let run = 0; run < SHORT_RUNS; run += 1)
    workgroups.push(await user.createWorkgroup(org, `Perf workgroup ${run + 1}`));
  const tasks: string[] = [];
  for (const wg of workgroups) {
    await poolReady(orchestrator, size);
    await expect(page.getByTestId('home.wg').locator(`option[value="${wg}"]`)).toHaveCount(1, { timeout: 30_000 });
    await page.getByTestId('home.wg').selectOption(wg);
    tasks.push(await sendPrompt(page, PROMPT));
    await expect(page.getByTestId('countdown.summary')).toContainText('Ready in', { timeout: 60_000 });
    await navTo(page, 'home');
  }
  const series = await evaluate(orchestrator, 'PERF-01 new workgroup', tasks);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 120_000 }).toBe(true);
  expect(series.summary.runs).toBe(SHORT_RUNS);
  expect(series.summary.passed, JSON.stringify(series.runs)).toBe(true);
  expect(series.runs.map((run) => run.estimate_ms)).toEqual(tasks.map(() => POOL_ESTIMATE_MS));
});
