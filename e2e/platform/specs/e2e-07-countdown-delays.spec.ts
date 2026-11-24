// E2E-07 (07-testing.md section 2.1, 06-web.md section 4.4, R-3.12): the countdown with real delays, T1 only.
// (a) Empty pool: the first task of a new org starts on the clone path; while its start is held up (the runner's link
//     to the hub is cut after the VM assignment began) the countdown reaches 0 and shows "Delayed" with the current
//     step and a live elapsed time; the steps fill once the link is back.
// (b) Runner stopped: a further task shows the real reason `device_offline` with an elapsed time, no digits.
// (c) Runner restarted: that task proceeds to working and is answered.
// The pool is restored to its size afterwards.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  expectAnswerInChat,
  expectStartOrder,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolSize,
  sendPrompt,
  transitions,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-07-countdown-delays.json', import.meta.url), 'utf8'),
);

test.describe.configure({ mode: 'serial' });

test('@E2E-07 @R-3.12 @R-P.5 @needs-vm the countdown shows the real status on the clone path, offline and after the restart', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-07 is a T1 test (07-testing.md section 2.1)');
  test.setTimeout(600_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  const size = await poolSize(orchestrator);
  try {
    // (a) clone path with an empty pool.
    expect(Object.values(await orchestrator.drainPool(0)).every((runner) => runner.size === 0)).toBe(true);
    await platformDevice(orchestrator);
    expect(Object.values(await orchestrator.pool()).map((pool) => pool.ready)).toEqual([0]);
    await authenticator('platform');
    await onboard(page, orchestrator, 'Lise Meitner');
    const first = await sendPrompt(page, 'Summarize the current state of fusion research');
    await expect(page.getByTestId('countdown.digits')).toBeVisible();
    await expect(page.getByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'done');
    await expect(page.getByTestId('countdown.step.vm-assigning')).toHaveAttribute('data-status', /current|done/, {
      timeout: 30_000,
    });
    await orchestrator.cut('runner-hub');
    try {
      await expect(page.getByTestId('countdown.delayed')).toHaveText('Delayed', { timeout: 30_000 });
      await expect(page.getByTestId('countdown.digits')).toHaveCount(0);
      const elapsed = page.getByTestId('countdown.elapsed');
      await expect(elapsed).toContainText(/ – \d+\.\d s$/);
      const before = await elapsed.textContent();
      await expect(elapsed).not.toHaveText(before!);
    } finally {
      await orchestrator.restore('runner-hub');
    }
    await expect(page.getByTestId('countdown.summary')).toHaveText(/^Ready in \d+\.\d s$/, { timeout: 90_000 });
    const [session] = await mainSessions(orchestrator, first);
    expectStartOrder(await transitions(orchestrator, session!.id));
    const [clone] = await orchestrator.hubQuery<{ seconds: number }>(
      `SELECT extract(epoch FROM r.at - a.at)::float8 AS seconds FROM hub.session_transition a
         JOIN hub.session_transition r ON r.session_id = a.session_id AND r.state = 'vm_ready'
        WHERE a.session_id = '${session!.id}' AND a.state = 'vm_assigning'`,
    );
    expect(clone!.seconds).toBeGreaterThan(2); // a clone boots; a pool VM is assigned in milliseconds
    await expectAnswerInChat(page, 'Fusion research summary.');

    // (b) runner stopped: the real reason instead of digits.
    await orchestrator.stopRunner('runner-1');
    await home(page);
    const second = await sendPrompt(page, 'Write a short plan for the migration');
    const reason = page.getByTestId('countdown.reason');
    await expect(reason).toHaveAttribute('data-reason', 'device_offline', { timeout: 60_000 });
    await expect(reason).toHaveText('The execution device is offline – the task starts as soon as it is back.');
    await expect(page.getByTestId('countdown.elapsed')).toContainText('Waiting for');
    await expect(page.getByTestId('countdown.digits')).toHaveCount(0);
    const [waiting] = await orchestrator.hubQuery<{ state: string; state_reason: string }>(
      `SELECT state, state_reason FROM hub.task WHERE id = '${second}'`,
    );
    expect(waiting).toEqual({ state: 'scheduling', state_reason: 'device_offline' });

    // (c) runner back: the task proceeds and is answered.
    await orchestrator.startRunner('runner-1');
    await expect(page.getByTestId('countdown.summary')).toContainText('Ready in', { timeout: 90_000 });
    await expectAnswerInChat(page, 'Migration plan.');
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok).toBe(true);
  } finally {
    const runners = await orchestrator.runners();
    if (Object.values(runners).includes('stopped')) await orchestrator.startRunner('runner-1');
    await orchestrator.drainPool(size);
  }
});
