// E2E-37 (07-testing.md section 2.1; 04-crypto.md section 4.7; 06-web.md sections 4, 7.2; 03-api.md section 4.6.1):
// workgroups and their selection. The admin creates a workgroup on the workgroup screen; the composer on the home
// screen preselects the default workgroup; the user selects the new workgroup and sends a prompt – the browser assigns
// the platform device to the new workgroup (`device_assign`) and the task runs in it; a further prompt sent without a
// selection runs in the default workgroup. Every task carries its workgroup (`task.wg_id`). Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { anonymousBudget, closingChecks, COST, navigate } from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
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
  readFileSync(new URL('../scenarios/e2e-37-workgroups-selection.json', import.meta.url), 'utf8'),
);
const WG_NAME = 'Design kz37-wg-z3x5c7';
const CANARIES = [
  'kz37-wg-z3x5c7',
  'kz37-prompt-s-v9b1n3',
  'kz37-prompt-d-m5q7w9',
  'kz37-answer-s8d2f4',
  'kz37-answer-g6h8j1',
];

test('@E2E-37 @R-2.4 @R-2.5 @R-2.9 @R-P.5 @needs-vm a task runs in the selected workgroup, without a selection in the default one', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator, 1);
  await authenticator('platform');
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, 'Ada Workgroups');
  const org = onboarded.org;
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const defaultWg = (await user.log(org)).state.defaultWg;

  // Every org has its default workgroup (genesis); the admin creates a further one on the workgroup screen.
  await navigate(page, `/o/${org}/workgroups`);
  await expect(page.getByTestId('workgroups.item')).toHaveCount(1);
  await page.getByTestId('workgroups.create.open').click();
  await page.getByTestId('workgroups.create.name').fill(WG_NAME);
  await page.getByTestId('workgroups.create.confirm').click();
  await expect(page.getByTestId('workgroups.item')).toHaveCount(2);
  const created = (await page.getByTestId('workgroups.item').filter({ hasText: WG_NAME }).getAttribute('data-wg'))!;
  const log = await user.log(org);
  expect(log.state.workgroups.has(created)).toBe(true);
  expect(log.state.wgAccess(user.userId, created)).toBe('write');

  // The composer preselects the default workgroup; the user selects the new one and sends.
  await home(page);
  const select = page.getByTestId('home.wg');
  await expect(select.locator('option', { hasText: WG_NAME })).toHaveCount(1, { timeout: 30_000 });
  await expect(select).toHaveValue(defaultWg);
  await select.selectOption({ label: WG_NAME });
  const selected = await sendPrompt(page, 'E2E37-SELECTED kz37-prompt-s-v9b1n3: hello');
  await expectAnswerInChat(page, 'Selected workgroup kz37-answer-s8d2f4.');

  // A further prompt without a selection: the composer is back on the default workgroup.
  await home(page);
  await expect(select).toHaveValue(defaultWg);
  const unselected = await sendPrompt(page, 'E2E37-DEFAULT kz37-prompt-d-m5q7w9: hello');
  await expectAnswerInChat(page, 'Default workgroup kz37-answer-g6h8j1.');

  const tasks = await orchestrator.hubQuery<{ id: string; wg_id: string }>(
    `SELECT id, wg_id::text FROM hub.task WHERE org_id = '${uuid(org)}' ORDER BY created_at`,
  );
  expect(tasks).toEqual([
    { id: selected, wg_id: created },
    { id: unselected, wg_id: defaultWg },
  ]);
  // the browser assigned the platform device to each workgroup at its first task
  const after = await user.log(org);
  expect(after.state.assigned(device, created)).toBe(true);
  expect(after.state.assigned(device, defaultWg)).toBe(true);
  for (const task of [selected, unselected]) {
    const [session] = await mainSessions(orchestrator, task);
    expect(session).toMatchObject({ device_id: device });
  }
  // the task list of each workgroup shows its own task
  await navigate(page, `/o/${org}/w/${created}`);
  await expect(page.getByTestId('tasks.row')).toHaveCount(1);
  await navigate(page, `/o/${org}/w/${defaultWg}`);
  await expect(page.getByTestId('tasks.row')).toHaveCount(1);
  await expectNoPlaintext(orchestrator, CANARIES);
  await closingChecks(orchestrator, true);
});
