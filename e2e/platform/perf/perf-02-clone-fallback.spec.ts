// PERF-02 (07-testing.md sections 2.2 and 5.3, R-3.10, R-14.3): time to working on the clone path. The pool is
// emptied and kept at 0; each run is the first task of a new org. Pass: every run and the p95 below 10 s. The pool is
// restored afterwards. Final acceptance (10 runs) by ACC-M1/PFM-01 with E2E_PERF_SHORT_RUNS; the default is 5 runs.
import { expect, test } from '../fixtures/test.ts';
import { platformDevice, poolSize } from '../fixtures/platform.ts';
import { CLONE_ESTIMATE_MS, evaluate, firstTaskInNewOrg, SHORT_RUNS, scenario } from './measure.ts';

test.describe.configure({ mode: 'serial' });

test('@PERF-02 @R-3.10 @R-14.3 @needs-vm clone fallback: with an empty pool the first task of a new org works in under 10 s', async ({
  browser,
  orchestrator,
  e2e,
}) => {
  test.setTimeout(SHORT_RUNS * 120_000 + 300_000);
  await orchestrator.loadScenario('model', scenario(SHORT_RUNS));
  await platformDevice(orchestrator);
  const size = await poolSize(orchestrator);
  try {
    await orchestrator.drainPool(0);
    await platformDevice(orchestrator);
    const tasks: string[] = [];
    for (let run = 0; run < SHORT_RUNS; run += 1) {
      expect(Object.values(await orchestrator.pool()).map((pool) => pool.ready)).toEqual([0]);
      tasks.push((await firstTaskInNewOrg(browser, e2e.hubOrigin, orchestrator, `Clone org ${run + 1}`)).task);
    }
    const series = await evaluate(orchestrator, 'PERF-02 clone fallback', tasks);
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 120_000 }).toBe(true);
    expect(series.summary.runs).toBe(SHORT_RUNS);
    expect(series.summary.passed, JSON.stringify(series.runs)).toBe(true);
    // AUD-01 A-30: right after the pool was emptied the hub already estimates the clone path (8000 ms, 03-api.md
    // section 4.6.2), not the 3000 ms of the pool it saw before (the early heartbeat after a VM state change).
    expect(series.runs.map((run) => run.estimate_ms)).toEqual(tasks.map(() => CLONE_ESTIMATE_MS));
  } finally {
    await orchestrator.drainPool(size);
  }
});
