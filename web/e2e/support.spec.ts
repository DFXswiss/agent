// The release of a workgroup to DFX support (ADR-096, KOR-56) against the real hub, on the desktop and the phones. The
// support person is made exactly as the operator does it: its identity certified in the platform log (`user_certify`),
// the role `support` granted with the root key (`role_grant`) and its display name set by a root-signed request. Without
// a release support sees nothing (404); a read release lets it read the workgroup in its support area but not write; a
// change to write lets it post a prompt, which the organization sees as "Support <Name>"; the organization's audit
// names its reads and writes and the admin gets the notifications; a revocation ends every read at once (404) and
// leaves support no wrap of the new epoch.
import type { BrowserContextOptions, Page } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { navigateInApp, onboard } from './helpers/onboard.ts';
import { appendPlatform, rootRequest } from './helpers/platform.ts';
import { PageUser } from './helpers/user.ts';
import { openChat } from './helpers/tasks.ts';

const SUPPORT_NAME = 'Sam Support';

// two users, a platform-log append and three release batches: more time than the default under a loaded test device
test.describe.configure({ timeout: 240_000 });

/** Opens `path` with a reload (the logs are read again); the identity unlocks with the passkey. */
async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

/** The hub's status of a refused request of `user` (`PageUser.request` throws with it), or 200. */
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
  await expect(page.getByTestId('support.section.done')).toBeVisible({ timeout: 30_000 });
}

test('a workgroup released to support: nothing before, read, write, the organization sees it, revocation', async ({
  page,
  baseURL,
  newContext,
}) => {
  const origin = baseURL!;
  // the organization: Ada with workgroup "Team" and a task
  await addAuthenticator(page);
  const ada = await onboard(page, { origin, name: 'Ada Admin' });
  const adaUser = await PageUser.open(page, ada.recoveryCode);
  const wg = await adaUser.createWorkgroup(ada.org, 'Team');
  const [task] = await adaUser.createTasks(ada.org, wg, ['Fix the login page']);
  // the support person: an ordinary account, certified and given the role by the operator
  const samPage = await (await newContext(test.info().project.use as BrowserContextOptions)).newPage();
  await addAuthenticator(samPage);
  const sam = await onboard(samPage, { origin, name: 'Sam' });
  const samUser = await PageUser.open(samPage, sam.recoveryCode);
  await appendPlatform(origin, [
    { type: 'user_certify', user: samUser.userId, pub: samUser.identity.public() },
    { type: 'role_grant', subject: samUser.userId, role: 'support' },
  ]);
  const named = await rootRequest(origin, 'PUT', `/v1/platform/support/${samUser.userId}`, { name: SUPPORT_NAME });
  expect(named.status).toBe(200);

  // no release: support sees nothing
  expect(await statusOf(samUser, 'GET', `/v1/workgroups/${wg}/tasks`)).toBe(404);
  expect(await statusOf(samUser, 'GET', `/v1/tasks/${task}`)).toBe(404);
  expect(await statusOf(samUser, 'GET', `/v1/orgs/${ada.org}/log`)).toBe(404);
  await visit(samPage, '/support');
  await expect(samPage.getByTestId('support.area.empty')).toBeVisible();

  // Ada releases the workgroup for reading, after the dialog
  await visit(page, `/o/${ada.org}/w/${wg}/settings`);
  await expect(page.getByTestId('support.section.state')).toHaveAttribute('data-release', 'none');
  await expect(page.getByTestId('support.section.people')).toContainText(`Support ${SUPPORT_NAME}`);
  await page.getByTestId('support.section.read').click();
  await expect(page.getByTestId('support.dialog.sees')).toContainText('Support sees every task');
  await expect(page.getByTestId('support.dialog.devices')).toContainText('DFX devices');
  await page.getByTestId('support.dialog.cancel').click();
  await change(page, 'support.section.read');
  await expect(page.getByTestId('support.section.state')).toHaveAttribute('data-release', 'read');

  // support reads: the release by its names, the task, the conversation – and cannot write
  await navigateInApp(samPage, '/support');
  await samPage.getByTestId('support.area.reload').click();
  // (the suite's other workers release workgroups of their own at the same time)
  const release = samPage.locator(`[data-testid="support.release"][data-wg="${wg}"]`);
  await release.getByTestId('support.release.open').click();
  await expect(samPage.getByTestId('support.failed')).toHaveCount(0);
  await expect(samPage.getByTestId('support.wg.access')).toHaveAttribute('data-access', 'read');
  await expect(samPage.getByTestId('support.wg.title')).toContainText('Team');
  await samPage.getByTestId('support.task.open').filter({ hasText: 'Fix the login page' }).click();
  await expect(samPage.getByTestId('chat.prompt.text')).toHaveText('Fix the login page');
  await expect(samPage.getByTestId('chat.prompt.author')).toHaveText('Ada Admin');
  await expect(samPage.getByTestId('support.task.read-only')).toBeVisible();
  await expect(samPage.getByTestId('support.composer')).toHaveCount(0);
  expect(await statusOf(samUser, 'POST', `/v1/tasks/${task}/prompts`, { event: {} })).toBe(403);

  // write: the composer appears with the next poll, support posts a prompt signed by itself
  await change(page, 'support.section.to-write');
  await expect(samPage.getByTestId('support.composer')).toBeVisible({ timeout: 30_000 });
  await samPage.getByTestId('support.composer.text').fill('Support looked at it');
  await samPage.getByTestId('support.composer.send').click();
  await expect(samPage.getByTestId('chat.prompt.text')).toHaveText(['Fix the login page', 'Support looked at it']);
  await expect(samPage.getByTestId('chat.prompt.author').last()).toHaveText('You');

  // the organization sees support: in the chat, in the audit, in the notifications
  await navigateInApp(page, `/o/${ada.org}/t/${task}`);
  await openChat(page); // the chat is the second tab (KONZEPT E-43)
  await expect(page.getByTestId('chat.prompt.author').last()).toHaveText(`Support ${SUPPORT_NAME}`, {
    timeout: 30_000,
  });
  await navigateInApp(page, `/o/${ada.org}/audit`);
  const write = page.getByTestId('audit.row').filter({ has: page.getByText('support.write', { exact: true }) });
  await expect(write.getByTestId('audit.row.actor')).toHaveText(new RegExp(`^Support ${SUPPORT_NAME} #[A-Z2-7]{4}$`));
  const read = page.getByTestId('audit.row').filter({ has: page.getByText('read.workgroup.tasks', { exact: true }) });
  await expect(read.first().getByTestId('audit.row.actor')).toHaveText(new RegExp(`^Support ${SUPPORT_NAME}`));
  await navigateInApp(page, '/notifications');
  const texts = page.getByTestId('notifications.text');
  await expect(texts.filter({ hasText: 'was released to DFX support (read)' })).toHaveCount(1);
  await expect(texts.filter({ hasText: 'was released to DFX support (read and write)' })).toHaveCount(1);
  // (the hub notes a first access per release state: the change to write starts another one)
  await expect(texts.filter({ hasText: 'for the first time' }).first()).toBeVisible();

  // revocation: support's next read is 404, it holds no wrap of the new epoch, its open view ends
  await navigateInApp(page, `/o/${ada.org}/w/${wg}/settings`);
  await change(page, 'support.section.revoke');
  await expect(page.getByTestId('support.section.state')).toHaveAttribute('data-release', 'none');
  expect(await statusOf(samUser, 'GET', `/v1/workgroups/${wg}/tasks`)).toBe(404);
  expect(await statusOf(samUser, 'GET', `/v1/tasks/${task}`)).toBe(404);
  const epoch = (await adaUser.log(ada.org)).state.epoch('wg', wg);
  expect(epoch).toBe(2);
  const wraps = (await samUser.request('GET', `/v1/wraps?scope=wg&scope_id=${wg}`)) as { wraps: { epoch: number }[] };
  expect(wraps.wraps.filter((wrap) => wrap.epoch === epoch)).toEqual([]);
  await expect(samPage.getByTestId('support.gone')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('notifications.text').first()).toBeAttached();
});
