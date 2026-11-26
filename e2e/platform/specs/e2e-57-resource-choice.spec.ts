// E2E-57 (07-testing.md section 2.1; 03-api.md section 4.6.2; ADR-075): resource choice with two devices – the run's
// platform device and a further runner instance on the runner device (a second certified platform device). A task in
// a workgroup without any device is created and waits for a device ("waiting for a device"); the task view offers
// "Assign a device", which assigns one and the task starts. The second device is given to the whole organization in
// one step (the dialog says what that means for the keys): it becomes a candidate of every workgroup, also of a
// workgroup created afterwards, and a member cannot point a workgroup at a device that is not assigned to it. In the
// default workgroup (only the organization's device) the first task gets a VM and the second one runs in the same VM:
// no fixed number of sessions per VM, the VM's measured free resources decide (ADR-075 addendum 2; waiting for room is
// E2E-38's). Finally the run's platform device joins the default workgroup and the user chooses it in the composer:
// the task runs there and the task view names device and VM. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  deviceItem,
  enrollRunner,
  expectDevice,
  navigate,
  removeRunner,
  spawnRunner,
} from '../fixtures/identity.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  invite,
  onboard,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-57-resource-choice.json', import.meta.url), 'utf8'),
);
const WAITING_WG = 'Waiting kz47-wg-w1e2r3';
const LATER_WG = 'Later kz47-wg-t4y5u6';

interface Placed {
  readonly device_id: string | null;
  readonly vm_id: string | null;
  readonly state: string;
}

test('@E2E-57 @R-0.3 @R-3.3 @needs-vm devices and VMs are chosen by capacity or by the user, a VM takes sessions while its resources last, and a missing device is a waiting state', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-57 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', scenario);
  const shared = await platformDevice(orchestrator);
  await authenticator('platform');
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, 'Ada Resources');
  const org = onboarded.org;
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const wg = (await user.log(org)).state.defaultWg;
  const sessionOf = async (task: string) =>
    (
      await orchestrator.hubQuery<Placed>(
        `SELECT device_id::text, vm_id, state FROM hub.session WHERE task_id = '${uuid(task)}' AND depth = 0
          ORDER BY created_at DESC, id DESC LIMIT 1`,
      )
    )[0]!;
  const taskOf = async (task: string) =>
    (
      await orchestrator.hubQuery<{ state: string; state_reason: string | null }>(
        `SELECT state, state_reason FROM hub.task WHERE id = '${uuid(task)}'`,
      )
    )[0]!;
  const RUNNER = (await spawnRunner(orchestrator, { pool_size: 1, memory_reserve_mib: 0 })).name;
  try {
    const own = await enrollRunner(orchestrator, RUNNER, (await invite(orchestrator, 'device')).code, true);
    expect(await operator(orchestrator, ['certify-pending', '--device', own])).toContain(`certified ${own}`);
    await expectDevice(orchestrator, own, { state: 'active', online: true });

    // Waiting for a device: a workgroup without devices; the task is created anyway and waits.
    const waitingWg = await user.createWorkgroup(org, WAITING_WG);
    const [waiting] = await user.createTasks(org, waitingWg, ['E2E47-WAIT kz47-prompt-q1w2e3: hello']);
    expect(await taskOf(waiting!)).toEqual({ state: 'scheduling', state_reason: 'no_device' });
    // deterministic: the first candidate by the documented order (online, free slots, free memory, smallest ID)
    const offered = (await user.request('GET', `/v1/workgroups/${waitingWg}/device-candidates`)) as {
      candidates: { device_id: string }[];
    };
    const first = offered.candidates[0]!.device_id;
    expect([shared, own]).toContain(first);
    await navigate(page, `/o/${org}/t/${waiting}`);
    await expect(page.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'no_device');
    await page.getByTestId('countdown.assign-device').click();
    await expectAnswerInChat(page, 'Assigned at last kz47-answer-a1b2c3.');
    const assignedTo = (await sessionOf(waiting!)).device_id!;
    expect(assignedTo).toBe(first);
    expect((await user.log(org)).state.assigned(assignedTo, waitingWg)).toBe(true);

    // The organization's device: one step for every workgroup, present and future.
    await navigate(page, `/o/${org}/devices`);
    await page.getByTestId('devices.assign.open').click();
    await page.getByTestId('devices.assign.scope').selectOption('org');
    await expect(page.getByTestId('devices.assign.org-notice')).toBeVisible();
    const candidate = page.locator(`[data-testid="devices.assign.candidate"][data-device="${own}"]`);
    await expect(candidate).toHaveAttribute('data-verified', 'true');
    await candidate.getByRole('radio').check();
    await page.getByTestId('devices.assign.confirm').click();
    await expect(page.getByTestId('devices.assign')).toHaveCount(0);
    await expect(deviceItem(page, own)).toHaveAttribute('data-assigned-org', 'true');
    const later = await user.createWorkgroup(org, LATER_WG);
    const log = await user.log(org);
    expect([log.state.assigned(own, wg), log.state.assigned(own, waitingWg), log.state.assigned(own, later)]).toEqual([
      true,
      true,
      true,
    ]);
    // nobody points a workgroup at a device that is not assigned to it
    if (!log.state.assigned(shared, later)) {
      await expect(
        user.request('PUT', `/v1/workgroups/${later}/placement`, { device_id: shared, vm_id: null }),
      ).rejects.toThrow(/403.*placement_forbidden/);
    }

    // No fixed number of sessions per VM: the second task runs in the first task's VM while its resources last.
    await poolReady(orchestrator, 1);
    await home(page);
    const firstTask = await sendPrompt(page, 'E2E47-FIRST kz47-prompt-a5s6d7: hello');
    await expectAnswerInChat(page, 'First VM kz47-answer-d4e5f6.');
    await expect.poll(async () => (await sessionOf(firstTask)).vm_id, { timeout: 60_000 }).not.toBeNull();
    const one = await sessionOf(firstTask);
    expect(one.device_id).toBe(own);
    await home(page);
    const second = await sendPrompt(page, 'E2E47-SECOND kz47-prompt-f8g9h1: hello');
    await expectAnswerInChat(page, 'Same VM kz47-answer-g7h8j9.');
    await expect.poll(async () => (await sessionOf(second)).vm_id, { timeout: 60_000 }).not.toBeNull();
    const two = await sessionOf(second);
    expect(two.device_id).toBe(own);
    expect(two.vm_id).toBe(one.vm_id);
    const local = await runnerStore<{ n: number }>(
      orchestrator,
      `SELECT count(*)::int AS n FROM runner.vm WHERE state <> 'deleted' AND wg_id = '${uuid(wg)}'`,
      RUNNER,
    );
    expect(local).toEqual([{ n: 1 }]);

    // Chosen by the user: the run's platform device joins the default workgroup and is chosen in the composer.
    await navigate(page, `/o/${org}/devices`);
    if (!(await user.log(org)).state.assigned(shared, wg)) {
      await page.getByTestId('devices.assign.open').click();
      await page.getByTestId('devices.assign.scope').selectOption(wg);
      const platform = page.locator(`[data-testid="devices.assign.candidate"][data-device="${shared}"]`);
      await expect(platform).toHaveAttribute('data-verified', 'true');
      await platform.getByRole('radio').check();
      await page.getByTestId('devices.assign.confirm').click();
      await expect(page.getByTestId('devices.assign')).toHaveCount(0);
    }
    await home(page);
    await page.getByTestId('home.device').selectOption(shared);
    const chosen = await sendPrompt(page, 'E2E47-CHOSEN kz47-prompt-m5n6b7: hello');
    await expectAnswerInChat(page, 'Chosen device kz47-answer-n4p5q6.');
    expect((await sessionOf(chosen)).device_id).toBe(shared);
    const [placement] = await orchestrator.hubQuery<{ device: string }>(
      `SELECT placement_device_id::text AS device FROM hub.task WHERE id = '${uuid(chosen)}'`,
    );
    expect(placement).toEqual({ device: shared });
    await expect(page.getByTestId('task.placement')).toHaveAttribute('data-device', shared, { timeout: 60_000 });
    await expectNoPlaintext(orchestrator, [
      'kz47-prompt-q1w2e3',
      'kz47-prompt-a5s6d7',
      'kz47-prompt-f8g9h1',
      'kz47-prompt-m5n6b7',
      'kz47-answer-a1b2c3',
      'kz47-answer-d4e5f6',
      'kz47-answer-g7h8j9',
      'kz47-answer-n4p5q6',
      'kz47-wg-w1e2r3',
      'kz47-wg-t4y5u6',
    ]);
  } finally {
    await removeRunner(orchestrator, RUNNER).catch(() => undefined);
  }
  await closingChecks(orchestrator, true);
});
