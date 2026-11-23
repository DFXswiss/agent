// E2E-11 (07-testing.md section 2.1; 05-runner.md section 3; KONZEPT section 3): one VM per workgroup. The org admin
// creates a second workgroup; one task in the default workgroup and one in the second run on the same device, each
// in its own VM: the hub's `vm` rows (device reports) name two VMs with distinct IDs, each assigned to exactly one of
// the workgroups, each session's `vm_id` at the hub is the VM of its workgroup, and its tmux server lives in the Lima
// instance the runner keeps for that VM. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore, sessionVm } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-11-vm-per-workgroup.json', import.meta.url), 'utf8'),
);
const WG_NAME = 'Research kz11-wg-c5x8z1q4';
const CANARIES = [
  'kz11-wg-c5x8z1q4',
  'kz11-prompt-d-r2t7y4u9',
  'kz11-prompt-s-i6o1p3a8',
  'kz11-answer-d9f3g6h1',
  'kz11-answer-s4j8k2l5',
  'kz11-name-e7w2q9r5',
];

interface VmRow {
  readonly id: string;
  readonly wg_id: string;
  readonly device_id: string;
  readonly state: string;
}

test.describe.configure({ mode: 'serial' });

// E2E-11 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-11 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-11 @R-3.7 @R-P.5 @needs-vm two workgroups of one org get two VMs on the same device', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator, 2);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz11-name-e7w2q9r5');

  const first = await sendPrompt(page, 'E2E11-DEFAULT kz11-prompt-d-r2t7y4u9: hello');
  await expectAnswerInChat(page, 'Default workgroup kz11-answer-d9f3g6h1.');

  // A second workgroup of the org (created by the admin's client), chosen on the home screen.
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const wg = await user.createWorkgroup(onboarded.org, WG_NAME);
  await home(page);
  await expect(page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(1, { timeout: 30_000 });
  await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
  const second = await sendPrompt(page, 'E2E11-SECOND kz11-prompt-s-i6o1p3a8: hello');
  await expectAnswerInChat(page, 'Second workgroup kz11-answer-s4j8k2l5.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

  const tasks = await orchestrator.hubQuery<{ id: string; wg_id: string; default_wg: string }>(
    `SELECT t.id, t.wg_id, o.default_wg FROM hub.task t JOIN hub.org o ON o.id = t.org_id
      WHERE t.org_id = '${uuid(onboarded.org)}' ORDER BY t.created_at`,
  );
  expect(tasks.map((task) => [task.id, task.wg_id === task.default_wg])).toEqual([
    [first, true],
    [second, false],
  ]);
  expect(tasks[1]!.wg_id).toBe(wg);
  const workgroups = tasks.map((task) => task.wg_id);

  // The hub's VM rows: two VMs on the device, one per workgroup.
  let vms: VmRow[] = [];
  await expect
    .poll(
      async () =>
        (vms = await orchestrator.hubQuery<VmRow>(
          `SELECT id, wg_id, device_id, state FROM hub.vm
            WHERE wg_id IN ('${uuid(workgroups[0]!)}', '${uuid(workgroups[1]!)}') AND state <> 'deleted' ORDER BY created_at`,
        )).length,
      { timeout: 60_000 },
    )
    .toBe(2);
  expect(new Set(vms.map((vm) => vm.id)).size).toBe(2);
  expect(vms.map((vm) => vm.wg_id).sort()).toEqual([...workgroups].sort());
  expect(vms.every((vm) => vm.device_id === device && vm.state === 'running')).toBe(true);

  // Each session runs in the VM of its workgroup: the hub's `session.vm_id` names the workgroup's VM (ACC-M1), and the
  // runner's store maps that VM ID to the Lima instance the session's tmux server lives in.
  const local = await runnerStore<{ id: string; lima_name: string; wg_id: string }>(
    orchestrator,
    `SELECT id, lima_name, wg_id::text FROM runner.vm WHERE state <> 'deleted' AND wg_id IS NOT NULL`,
  );
  for (const [index, task] of [first, second].entries()) {
    const [session] = await mainSessions(orchestrator, task);
    expect(session!.device_id).toBe(device);
    const vm = vms.find((row) => row.wg_id === workgroups[index])!;
    const [row] = await orchestrator.hubQuery<{ vm_id: string | null }>(
      `SELECT vm_id::text FROM hub.session WHERE id = '${uuid(session!.id)}'`,
    );
    expect(row).toEqual({ vm_id: vm.id });
    const where = await sessionVm(orchestrator, session!.id);
    expect(local.find((item) => item.id === vm.id)).toEqual({
      id: vm.id,
      lima_name: where.vm,
      wg_id: workgroups[index],
    });
  }

  await expectNoPlaintext(orchestrator, CANARIES);
});
