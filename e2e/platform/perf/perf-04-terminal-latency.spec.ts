// PERF-04 (07-testing.md section 2.2, 05-runner.md section 7, R-6.9): terminal live latency recorder flush → xterm
// render, p95 < 1 s over `E2E_PERF_TERMINAL_EVENTS` chunks (default 300). The open terminal view of a session follows
// terminal output the orchestrator writes into the pane (`perf/latency.ts`); each chunk's render time (two animation
// frames after the view applied it) is matched with its `session.terminal` event, whose time is the recorder flush in
// the VM (corrected by the measured VM clock offset). Writes `perf-terminal-latency.csv` and `perf-latency.jsonl` (with
// the device load) into the run's artefacts.
import { expect, test } from '../fixtures/test.ts';
import {
  type Evaluation,
  MARK_INTERVAL_MS,
  openTerminal,
  renders,
  report,
  startMarks,
  TERMINAL_EVENTS,
  terminalEvents,
  watchRenders,
} from './latency.ts';

test.describe.configure({ mode: 'serial' });

test('@PERF-04 @R-6.9 @needs-vm terminal latency recorder flush → xterm render: p95 under 1 s', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(TERMINAL_EVENTS * 1.15 * MARK_INTERVAL_MS + 600_000);
  await authenticator('platform');
  const session = await openTerminal(page, orchestrator, 'Perf terminal');
  const opened = await watchRenders(page);
  const writing = await startMarks(orchestrator, session, TERMINAL_EVENTS);
  await page.waitForTimeout(writing + 3000);
  // every chunk the hub has is applied in the view (the newest flush may still be on its way)
  const view = page.getByTestId('terminal');
  await expect
    .poll(async () => Number(await view.getAttribute('data-chunks')) - (await terminalEvents(orchestrator, session)), {
      timeout: 60_000,
      intervals: [1000],
    })
    .toBe(0);
  await page.waitForTimeout(500);
  const evaluation = await orchestrator.call<Evaluation>('POST', '/latency/terminal', {
    label: `PERF-04 ${e2e.topology}`,
    session,
    opened_ms: opened,
    renders: await renders(page),
  });
  report(`PERF-04 ${e2e.topology}`, evaluation);
  expect(evaluation.summary.samples).toBeGreaterThanOrEqual(TERMINAL_EVENTS);
  expect(evaluation.summary.passed, JSON.stringify(evaluation.summary)).toBe(true);
});
