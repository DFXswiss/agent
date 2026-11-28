// E2E-72 (KOR-56, ADR-096; 04-crypto.md sections 4.7, 4.8, 6.3, 6.4; 03-api.md section 4.13; 06-web.md section
// 12.8a): a workgroup released to DFX support, through the whole chain. Ada works in her private organization on the
// platform device; Sam has an account of his own and gets the platform role `support` from the operator CLI
// (`certify-user`, `grant-role support --name`).
// - Without a release Sam sees nothing (404), his support area is empty, and a wrap to himself is refused.
// - A read release (after the dialog with what support sees, "read stays read" and the DFX-device note): Sam reads the
//   task with Ada's prompt and the model's answer in his support area; writing is refused (403).
// - A change to write: Sam's prompt reaches the running conversation, the model answers it, and Ada sees the prompt
//   as "Support <Name>".
// - The organization sees support: its audit names Sam's reads and writes as "Support <Name> #XXXX", Ada is notified
//   of the releases and of the first access.
// - Revocation: one batch with the rotation; Sam's next read is 404, he holds no wrap of the new epoch, and content
//   written afterwards stays closed to his keys. The hub plaintext scan stays green.
// Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { closingChecks, newBrowser, visit } from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  invite,
  onboard,
  onboardWithCode,
  openChat,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import type { Page } from '@playwright/test';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-72-support-release.json', import.meta.url), 'utf8'),
);
const SUPPORT_NAME = 'Sam kz69-name-s5h2';
const ADA_PROMPT = 'E2E69-ADA kz69-prompt-a-r4t8: check the login page';
const SUPPORT_PROMPT = 'E2E69-SUPPORT kz69-prompt-s-w2c9: try the second form field first';
const AFTER = 'After the revocation kz69-after-v8n3';

/** The hub's status of a request of `user` (`PageUser.request` throws with it), or 200. */
async function statusOf(user: PageUser, method: string, target: string, body?: unknown): Promise<number> {
  try {
    await user.request(method, target, body);
    return 200;
  } catch (error) {
    return Number(/ (\d{3}) /.exec(String(error))![1]);
  }
}

/** Confirms a change in the support section of the workgroup settings. */
async function change(page: Page, button: string): Promise<void> {
  await page.getByTestId(button).click();
  await expect(page.getByTestId('support.dialog.kept')).toBeVisible();
  await page.getByTestId('support.dialog.confirm').click();
  await expect(page.getByTestId('support.section.done')).toBeVisible({ timeout: 60_000 });
}

test('@E2E-72 @R-8.5 @R-8.6 @R-8.9 @R-5.4 @needs-vm a workgroup released to support: nothing before, read, write through the session, the organization sees it, revocation closes new content', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_200_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz69-name-a7b1');
  const org = onboarded.org;
  const ada = await PageUser.open(page, onboarded.recoveryCode);
  const wg = (await ada.log(org)).state.defaultWg;
  await home(page);
  const task = await sendPrompt(page, ADA_PROMPT);
  await expectAnswerInChat(page, 'The login page is checked kz69-answer-a-m3p6.');

  // Sam: an account of his own, certified and made support by the operator.
  const samBrowser = await newBrowser(browser, e2e);
  const sam = samBrowser.page;
  const samOnboarded = await onboardWithCode(sam, (await invite(orchestrator, 'user')).code, 'Sam');
  const samUser = await PageUser.open(sam, samOnboarded.recoveryCode);
  expect(await operator(orchestrator, ['certify-user', '--user', samUser.userId])).toContain('certified user');
  expect(
    await operator(orchestrator, ['grant-role', 'support', '--user', samUser.userId, '--name', SUPPORT_NAME]),
  ).toContain(`is named ${SUPPORT_NAME}`);

  // No release: nothing, and no wrap he could give himself.
  expect(await statusOf(samUser, 'GET', `/v1/workgroups/${wg}/tasks`)).toBe(404);
  expect(await statusOf(samUser, 'GET', `/v1/tasks/${task}`)).toBe(404);
  expect(await statusOf(samUser, 'GET', `/v1/orgs/${org}/log`)).toBe(404);
  await visit(sam, '/support');
  await expect(sam.getByTestId('support.area.empty')).toBeVisible({ timeout: 30_000 });

  // Ada releases the workgroup for reading, after the dialog.
  await visit(page, `/o/${org}/w/${wg}/settings`);
  await expect(page.getByTestId('support.section.people')).toContainText(`Support ${SUPPORT_NAME}`, {
    timeout: 30_000,
  });
  await page.getByTestId('support.section.read').click();
  await expect(page.getByTestId('support.dialog.sees')).toBeVisible();
  await expect(page.getByTestId('support.dialog.devices')).toBeVisible();
  await page.getByTestId('support.dialog.cancel').click();
  await change(page, 'support.section.read');
  await expect(page.getByTestId('support.section.state')).toHaveAttribute('data-release', 'read');

  // Sam reads the task and the model's answer; writing is refused.
  await navigateInApp(sam, '/support');
  await sam.getByTestId('support.area.reload').click();
  await sam.locator(`[data-testid="support.release"][data-wg="${wg}"]`).getByTestId('support.release.open').click();
  await expect(sam.getByTestId('support.wg.access')).toHaveAttribute('data-access', 'read', { timeout: 60_000 });
  await sam.getByTestId('support.task.open').first().click();
  await expect(sam.getByTestId('chat.prompt.text').first()).toHaveText(ADA_PROMPT, { timeout: 60_000 });
  await expect(sam.getByText('The login page is checked kz69-answer-a-m3p6.')).toBeVisible({ timeout: 60_000 });
  await expect(sam.getByTestId('support.composer')).toHaveCount(0);
  expect(await statusOf(samUser, 'POST', `/v1/tasks/${task}/prompts`, { event: {} })).toBe(403);

  // Write: Sam's prompt reaches the conversation and is answered; Ada sees it as support's.
  await change(page, 'support.section.to-write');
  await expect(sam.getByTestId('support.composer')).toBeVisible({ timeout: 60_000 });
  await sam.getByTestId('support.composer.text').fill(SUPPORT_PROMPT);
  await sam.getByTestId('support.composer.send').click();
  await navigateInApp(page, `/o/${org}/t/${task}`);
  await openChat(page); // the chat is the second tab (KONZEPT E-43)
  await expectAnswerInChat(page, "Support's hint is applied kz69-answer-s-q7d1.");
  await expect(page.getByTestId('chat.prompt.author').last()).toHaveText(`Support ${SUPPORT_NAME}`);
  const authored = await orchestrator.hubQuery<{ author_id: string }>(
    `SELECT author_id::text FROM hub.event WHERE task_id = '${uuid(task)}' AND kind = 'prompt' ORDER BY scope_seq`,
  );
  expect(authored.map((row) => row.author_id)).toEqual([ada.userId, samUser.userId]);

  // The organization sees support in its audit and in the notifications.
  await navigateInApp(page, `/o/${org}/audit`);
  const write = page.getByTestId('audit.row').filter({ has: page.getByText('support.write', { exact: true }) });
  await expect(write.first().getByTestId('audit.row.actor')).toHaveText(
    new RegExp(`^Support ${SUPPORT_NAME} #[A-Z2-7]{4}$`),
    { timeout: 30_000 },
  );
  const read = page.getByTestId('audit.row').filter({ has: page.getByText('read.workgroup.tasks', { exact: true }) });
  await expect(read.first().getByTestId('audit.row.actor')).toHaveText(new RegExp(`^Support ${SUPPORT_NAME}`));
  await navigateInApp(page, '/notifications');
  const texts = page.getByTestId('notifications.text');
  await expect(texts.filter({ hasText: 'for the first time' })).toHaveCount(1);
  await expect(texts.filter({ hasText: 'released to DFX support' }).first()).toBeVisible();

  // Revocation: Sam is out at once and holds no wrap of the new epoch; new content stays closed to him.
  await navigateInApp(page, `/o/${org}/w/${wg}/settings`);
  await change(page, 'support.section.revoke');
  await expect(page.getByTestId('support.section.state')).toHaveAttribute('data-release', 'none');
  expect(await statusOf(samUser, 'GET', `/v1/workgroups/${wg}/tasks`)).toBe(404);
  expect(await statusOf(samUser, 'GET', `/v1/tasks/${task}`)).toBe(404);
  const epoch = (await ada.log(org)).state.epoch('wg', wg);
  expect(epoch).toBe(2);
  const samWraps = await orchestrator.hubQuery<{ epoch: number }>(
    `SELECT epoch FROM hub.key_wrap WHERE recipient_id = '${uuid(samUser.userId)}' AND scope = 'wg'` +
      ` AND scope_id = '${uuid(wg)}'`,
  );
  expect(samWraps).toEqual([]);
  await navigateInApp(page, `/o/${org}/w/${wg}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(AFTER);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: AFTER })).toBeVisible({ timeout: 30_000 });
  expect((await samUser.keys(org, await ada.log(org))).size).toBe(0);
  await expect(sam.getByTestId('support.gone')).toBeVisible({ timeout: 60_000 });

  await expectNoPlaintext(orchestrator, [ADA_PROMPT, SUPPORT_PROMPT, AFTER]);
  await closingChecks(orchestrator, true);
  await samBrowser.context.close();
});
