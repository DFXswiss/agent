// PERF-03 (07-testing.md section 2.2, 03-api.md section 5, R-6.2, R-6.9, R-12.6): event latency runner outbox write →
// browser frame, p95 < 300 ms over at least 1000 events (`E2E_PERF_EVENTS`), on T1 and T2 (LAN). The events are the
// recorder flushes of terminal output the orchestrator writes into the session's pane while the terminal view is open
// (`perf/latency.ts`); every frame of the browser's live stream that carries a device event counts. The orchestrator
// corrects the runner device's clock by the measured offset and writes `perf-event-latency.csv` and
// `perf-latency.jsonl` (with the device load) into the run's artefacts.
import { expect, test } from '../fixtures/test.ts';
import {
  EVENTS,
  type Evaluation,
  framesSince,
  MARK_INTERVAL_MS,
  openTerminal,
  report,
  startMarks,
  tapStream,
} from './latency.ts';

test.describe.configure({ mode: 'serial' });

test('@PERF-03 @R-6.2 @R-6.9 @R-12.6 @needs-vm event latency runner outbox → browser frame: p95 under 300 ms', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(EVENTS * 1.15 * MARK_INTERVAL_MS + 600_000);
  await tapStream(page);
  await authenticator('platform');
  const session = await openTerminal(page, orchestrator, 'Perf events');
  const since = Date.now();
  const writing = await startMarks(orchestrator, session, EVENTS);
  await expect
    .poll(async () => (await framesSince(page, since)).length, { timeout: writing + 120_000, intervals: [2000] })
    .toBeGreaterThanOrEqual(EVENTS);
  // the writer's last lines and their frames
  await page.waitForTimeout(Math.max(0, since + writing - Date.now()) + 3000);
  const frames = await framesSince(page, since);
  const evaluation = await orchestrator.call<Evaluation>('POST', '/latency/events', {
    label: `PERF-03 ${e2e.topology}`,
    since_ms: since,
    frames,
  });
  report(`PERF-03 ${e2e.topology}`, evaluation);
  expect(evaluation.summary.samples).toBeGreaterThanOrEqual(EVENTS);
  expect(evaluation.summary.passed, JSON.stringify(evaluation.summary)).toBe(true);
});
