// Reconnect latency (09-milestones.md WP-M3-03; PFM-01), measured in the scenario of E2E-14 and filed with PERF-03,
// the event-latency test it extends (R-6.2, R-6.9; 03-api.md sections 5 and 6.2, 06-web.md section 12.5). During a
// running turn the links runner ↔ hub and browser ↔ hub are cut; the turn finishes offline (the outbox grows). Then:
//
// - runner: from restoring the runner's link until the hub has stored the last event of the backlog (hub clock; the
//   spec runs on the hub device in both topologies, so its clock is the hub's), split into the reconnect wait (until
//   the first backlog event is stored) and the catch-up (first to last backlog event);
// - browser: from restoring the browser's link until the chat shows every output of the turn, split into the
//   reconnect wait (until the first missing output appears) and the catch-up (first to all outputs).
//
// The waits are the clients' capped backoff (1 s → 30 s ±20 %, 03-api.md section 6.2, 06-web.md section 12.5) and are
// bounded by that cap plus 4 s (`RECONNECT_LIMIT_MS`); the catch-up – the work after the link is back – is bounded by
// `CATCH_UP_LIMIT_MS`. All series go to `perf-reconnect.csv`.
// `E2E_PERF_RECONNECT_RUNS` turns per iteration (default 3). T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { runnerStore } from '../fixtures/guest.ts';
import { home, mainSessions, onboard, platformDevice, poolReady, sendPrompt, uuid } from '../fixtures/platform.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { load, series } from './parallel.ts';

const RUNS = Number(process.env.E2E_PERF_RECONNECT_RUNS ?? '3');
/** Backoff cap 30 s + 20 % jitter (03-api.md section 6.2, 06-web.md section 12.5) plus 4 s to catch up. */
const RECONNECT_LIMIT_MS = 40_000;
/** Catch-up of a backlog of one turn once the link is back (a few seconds of pushes and one gap pull). */
const CATCH_UP_LIMIT_MS = 5_000;
const OUTPUTS = 6;

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-14-offline-reconnect.json', import.meta.url), 'utf8'),
);

const pending = async (orchestrator: Orchestrator): Promise<{ pending: number; max: number }> =>
  (
    await runnerStore<{ pending: number; max: number }>(
      orchestrator,
      `SELECT (count(*) FILTER (WHERE pushed_at IS NULL))::int AS pending, coalesce(max(origin_seq), 0)::int AS max
         FROM runner.outbox`,
    )
  )[0]!;

const hubOutputs = async (orchestrator: Orchestrator, session: string): Promise<number> =>
  (
    await orchestrator.hubQuery<{ n: number }>(
      `SELECT count(*)::int AS n FROM hub.event WHERE session_id = '${uuid(session)}' AND kind = 'session.output'`,
    )
  )[0]!.n;

/** Hub receive times (epoch ms) of the first and last event of the backlog (`after` < origin_seq <= `upTo`). */
const backlogStored = async (
  orchestrator: Orchestrator,
  device: string,
  after: number,
  upTo: number,
): Promise<{ first: number | null; last: number | null }> =>
  (
    await orchestrator.hubQuery<{ first: number | null; last: number | null }>(
      `SELECT (extract(epoch FROM min(received_at)) * 1000)::float8 AS first,
              (extract(epoch FROM max(received_at)) * 1000)::float8 AS last FROM hub.event
        WHERE origin_device_id = '${uuid(device)}' AND origin_seq > ${Math.trunc(after)}
          AND origin_seq <= ${Math.trunc(upTo)}`,
    )
  )[0]!;

test.describe.configure({ mode: 'serial' });

test('@PERF-03 @R-6.2 @R-6.9 @needs-vm reconnect latency: runner and browser catch up after an outage within the backoff cap', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(RUNS * 420_000 + 300_000);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Perf reconnect');
  const runnerWait: number[] = [];
  const runnerCatchUp: number[] = [];
  const browserWait: number[] = [];
  const browserCatchUp: number[] = [];
  const outage: number[] = [];
  const topology = e2e.topology.toUpperCase();
  await load(orchestrator, `before reconnect ${topology}`);
  for (let run = 1; run <= RUNS; run += 1) {
    await orchestrator.loadScenario('model', scenario);
    const task = await sendPrompt(page, `E2E14-OFFLINE perf reconnect ${run}: write five files, one by one`);
    let session = '';
    await expect.poll(async () => (session = (await mainSessions(orchestrator, task))[0]?.id ?? '')).not.toBe('');
    await expect.poll(() => hubOutputs(orchestrator, session), { timeout: 90_000 }).toBeGreaterThanOrEqual(1);
    await orchestrator.cut('runner-hub');
    await orchestrator.cut('browser-hub');
    const cutAt = Date.now();
    try {
      const atCut = await pending(orchestrator);
      // The turn finishes offline; the backlog stays in the outbox.
      await expect
        .poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 90_000, intervals: [1000] })
        .toBe(true);
      await expect.poll(async () => (await pending(orchestrator)).pending, { timeout: 30_000 }).toBeGreaterThan(0);
      const backlog = await pending(orchestrator);
      expect(backlog.max).toBeGreaterThan(atCut.max);

      const runnerRestored = Date.now();
      outage.push(runnerRestored - cutAt);
      await orchestrator.restore('runner-hub');
      await expect
        .poll(async () => (await pending(orchestrator)).pending, { timeout: 120_000, intervals: [100] })
        .toBe(0);
      const stored = await backlogStored(orchestrator, device, atCut.max, backlog.max);
      expect(stored.first).not.toBeNull();
      runnerWait.push(stored.first! - runnerRestored);
      runnerCatchUp.push(stored.last! - stored.first!);

      const shown = await page.getByTestId('chat.output').count();
      expect(shown).toBeLessThan(OUTPUTS);
      const outputs = (count: number) =>
        page.waitForFunction((n) => document.querySelectorAll('[data-testid="chat.output"]').length >= n, count, {
          polling: 20,
          timeout: 120_000,
        });
      const browserRestored = Date.now();
      await orchestrator.restore('browser-hub');
      await outputs(shown + 1);
      const firstShown = Date.now();
      await outputs(OUTPUTS);
      browserWait.push(firstShown - browserRestored);
      browserCatchUp.push(Date.now() - firstShown);
      await expect(page.getByTestId('chat.output')).toHaveCount(OUTPUTS);
    } finally {
      await orchestrator.restore('runner-hub');
      await orchestrator.restore('browser-hub');
    }
    await home(page);
  }
  await load(orchestrator, `after reconnect ${topology}`);
  await series(orchestrator, 'reconnect', `outage ${topology}`, outage);
  const checked = [
    await series(orchestrator, 'reconnect', `runner reconnect wait ${topology}`, runnerWait, RECONNECT_LIMIT_MS),
    await series(orchestrator, 'reconnect', `runner catch-up ${topology}`, runnerCatchUp, CATCH_UP_LIMIT_MS),
    await series(orchestrator, 'reconnect', `browser reconnect wait ${topology}`, browserWait, RECONNECT_LIMIT_MS),
    await series(orchestrator, 'reconnect', `browser catch-up ${topology}`, browserCatchUp, CATCH_UP_LIMIT_MS),
  ];
  expect(
    checked.map((item) => item.passed),
    JSON.stringify({ runnerWait, runnerCatchUp, browserWait, browserCatchUp }),
  ).toEqual([true, true, true, true]);
});
