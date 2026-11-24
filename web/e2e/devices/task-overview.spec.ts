// Task overview of an organization (KOR-32) against the real hub: the "Tasks" entry directly below Home (sidebar on
// the desktop, bottom bar on the phones), all tasks of the workgroups the user can read newest activity first with
// their workgroup, the filters (workgroup, only mine, title search), a click opens the task, and the empty state says
// how a task is created. Two workgroups with different rights: a member reads "Research" but has no access to
// "Secret" – Secret's tasks never appear, it is not offered as a filter and the browser never requests it.
import { expect, type Page } from '@playwright/test';
import { test } from '../fixtures/test.ts';
import { addAuthenticator } from '../helpers/authenticator.ts';
import { joinWithCode } from '../helpers/members.ts';
import { onboard } from '../helpers/onboard.ts';
import { freshUser, sendPrompt } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

/** Opens the overview the way a user does: the bottom bar on a phone, else the main navigation. */
async function openTasks(page: Page): Promise<void> {
  const bottom = page.getByTestId('shell.bottom.tasks');
  if (await bottom.isVisible()) await bottom.click();
  else await page.getByTestId('shell.nav.tasks').click();
  await expect(page).toHaveURL(/\/o\/[0-9a-f-]{36}\/tasks$/);
  await expect(page.getByTestId('tasks.overview')).toBeVisible();
}

/** Opens `path` with a reload: the identity is locked again and unlocks with the passkey. */
async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

test('the overview explains how to create a task, then lists it; Tasks sits directly below Home', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  if (await page.getByTestId('shell.nav').isVisible()) {
    const entries = page.getByTestId('shell.nav').locator('[data-testid^="shell.nav."]');
    await expect(entries.nth(0)).toHaveAttribute('data-testid', 'shell.nav.home');
    await expect(entries.nth(1)).toHaveAttribute('data-testid', 'shell.nav.tasks');
    // Home is the current page only on its own path
    await expect(page.getByTestId('shell.nav.home')).toHaveAttribute('aria-current', 'page');
  }
  await openTasks(page);
  const nav = page.getByTestId('shell.bottom.tasks').or(page.getByTestId('shell.nav.tasks'));
  await expect(nav.filter({ visible: true })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByTestId('shell.nav.home')).not.toHaveAttribute('aria-current', 'page');
  await expect(page.getByTestId('tasks.empty')).toContainText('To create a task, open Home');
  await page.getByTestId('tasks.overview.create').click();
  await sendPrompt(page, 'Summarize the open invoices');
  await openTasks(page);
  const rows = page.getByTestId('tasks.row');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Summarize the open invoices']);
  await expect(rows.getByTestId('tasks.row.wg')).toHaveText(['Default']);
  // the overview is a link of its own: a reload unlocks again and shows it after the log is verified
  await visit(page, `/o/${user.org}/tasks`);
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Summarize the open invoices']);
  await rows.first().click();
  await expect(page.getByTestId('task.title')).toHaveText('Summarize the open invoices');
});

test('a member sees the tasks of the workgroups they can read, never those of another one @R-8.4', async ({
  page,
  baseURL,
  newContext,
}) => {
  test.setTimeout(150_000);
  const origin = baseURL!;
  // Ada (an admin at a desktop, also in the phone projects): a private org with a platform device; her first task
  // assigns it to the default workgroup
  const adaPage = await (
    await newContext({ viewport: { width: 1280, height: 720 }, isMobile: false, hasTouch: false })
  ).newPage();
  const ada = await freshUser(adaPage, origin);
  await sendPrompt(adaPage, 'Plan the product launch');
  const adaUser = await PageUser.open(adaPage, ada.recoveryCode);
  await adaPage.getByTestId('shell.nav.members').click();
  await adaPage.getByTestId('members.invite.open').click();
  await adaPage.getByTestId('members.invite.name').fill('Invited person');
  await adaPage.getByTestId('members.invite.submit').click();
  const code = (await adaPage.getByTestId('members.invite.code').textContent())!;
  await adaPage.getByTestId('members.invite.done').click();
  // Bob (this test's page and viewport) joins Ada's org as a member: write in the default workgroup only
  await addAuthenticator(page);
  const bob = await onboard(page, { origin, name: 'Bob Member' });
  const bobUser = await PageUser.open(page, bob.recoveryCode);
  await joinWithCode(bobUser, origin, code, 'Bob Member');
  // Research: Bob may read; Secret: Bob has no access. Both have the device and one task each.
  const research = await adaUser.createWorkgroup(ada.org, 'Research', 'write', {
    readers: [bobUser.userId],
    assignDevices: true,
  });
  const secret = await adaUser.createWorkgroup(ada.org, 'Secret', 'write', { assignDevices: true });
  await adaUser.createTasks(ada.org, secret, ['Salary list of the secret team']);
  await adaUser.createTasks(ada.org, research, ['Market report for Switzerland']);

  const requested: string[] = [];
  page.on('request', (request) => requested.push(new URL(request.url()).pathname));
  await visit(page, `/o/${ada.org}`);
  await sendPrompt(page, 'Weekly report for the team');
  await openTasks(page);
  const rows = page.getByTestId('tasks.row');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText([
    'Weekly report for the team',
    'Market report for Switzerland',
    'Plan the product launch',
  ]);
  await expect(rows.getByTestId('tasks.row.wg')).toHaveText(['Default', 'Research', 'Default']);
  await expect(rows.getByTestId('tasks.row.owner')).toHaveText(['Bob Member', 'Ada Lovelace', 'Ada Lovelace']);
  await expect(page.getByTestId('tasks.filter.wg').locator('option')).toHaveText([
    'All workgroups',
    'Default',
    'Research',
  ]);
  expect(requested).toContain(`/v1/workgroups/${research}/tasks`);
  expect(requested.filter((path) => path.includes(secret))).toEqual([]);

  // only mine, the title search and the workgroup filter
  await page.getByTestId('tasks.filter.mine').check();
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Weekly report for the team']);
  await page.getByTestId('tasks.filter.mine').uncheck();
  await page.getByTestId('tasks.search').fill('report');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText([
    'Weekly report for the team',
    'Market report for Switzerland',
  ]);
  await page.getByTestId('tasks.filter.wg').selectOption(research);
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Market report for Switzerland']);
  await page.getByTestId('tasks.search').fill('launch');
  await expect(page.getByTestId('tasks.empty')).toContainText('No task matches the filters.');
  await page.getByTestId('tasks.filter.reset').click();
  await expect(rows).toHaveCount(3);
  await rows.filter({ hasText: 'Market report' }).click();
  await expect(page.getByTestId('task.title')).toHaveText('Market report for Switzerland');
  expect(requested.filter((path) => path.includes(secret))).toEqual([]);
});

test('the overview loads the next page across workgroups when the end of the list becomes visible', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(120_000);
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  await sendPrompt(page, 'Task 0');
  const api = await PageUser.open(page, user.recoveryCode);
  const research = await api.createWorkgroup(user.org, 'Research', 'write', { assignDevices: true });
  await api.createTasks(
    user.org,
    research,
    Array.from({ length: 55 }, (_, index) => `Research ${index + 1}`),
  );
  await visit(page, `/o/${user.org}/tasks`);
  const rows = page.getByTestId('tasks.row');
  // the first page of Research (50 rows) binds: the older task of Default waits until Research's next page is loaded
  await expect(rows).toHaveCount(50);
  await expect(rows.first().getByTestId('tasks.row.title')).toHaveText('Research 55');
  await expect(rows.first().getByTestId('tasks.row.wg')).toHaveText('Research');
  // no infinite scroll (ADR-086 section 1): the next page loads on "Load more"
  await page.getByTestId('tasks.more').click();
  await expect(rows).toHaveCount(56);
  await expect(rows.last().getByTestId('tasks.row.title')).toHaveText('Task 0');
  await expect(rows.last().getByTestId('tasks.row.wg')).toHaveText('Default');
  await expect(page.getByTestId('tasks.more')).toHaveCount(0);
});
