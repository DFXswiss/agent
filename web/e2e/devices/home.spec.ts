// Home screen and the first task (06-web.md sections 4.0-4.3) against the real hub and the device double: the first
// task assigns a platform device, chips fill and never send, the workgroup choice lists only writable workgroups. The
// composer stays at the top of the situation view (KOR-39); the view itself has its own spec (`attention.spec.ts`).
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

test('the first task assigns a platform device and lands on the task page', async ({ page, baseURL }) => {
  const posts: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') posts.push(new URL(request.url()).pathname);
  });
  const user = await freshUser(page, baseURL!);
  await expect(page.getByTestId('home.screen')).toBeVisible();
  await expect(page.getByTestId('home.org')).toHaveText('Private');
  await expect(page.getByTestId('home.wg')).toHaveValue(/.+/);
  // a new organization: every group of the situation view is empty and the suggestions are open
  await expect(page.getByTestId('attention.empty')).toHaveCount(4);
  await expect(page.getByTestId('composer.chips')).toBeVisible();
  const task = await sendPrompt(page, 'What is 2+2?');
  expect(posts).toEqual(expect.arrayContaining([`/v1/orgs/${user.org}/log`, '/v1/wraps', '/v1/tasks']));
  expect(posts.indexOf(`/v1/orgs/${user.org}/log`)).toBeLessThan(posts.indexOf('/v1/tasks'));
  await expect(page.getByTestId('task.title')).toHaveText('What is 2+2?');
  await expect(page.getByTestId('chat.prompt.text')).toHaveText('What is 2+2?');
  await expect(page.getByTestId('countdown.step.assigning')).toHaveAttribute('data-status', 'done');
  const session = await mainSession(task);
  expect(session.device_id).toBe(user.device);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 50 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
  // the second task needs no assignment
  await page.getByTestId('shell.nav.home').click();
  await expect(page.getByTestId('attention.item')).toHaveCount(1);
  await expect(page.getByTestId('attention.item').getByTestId('tasks.row.title')).toHaveText('What is 2+2?');
  // with a task the suggestions wait behind their toggle
  await expect(page.getByTestId('composer.chips')).toHaveCount(0);
  await page.getByTestId('home.suggestions').click();
  await expect(page.getByTestId('composer.chips')).toBeVisible();
  posts.length = 0;
  await page.getByTestId('composer.prompt').fill('And 3+3?');
  await page.keyboard.press('Control+Enter');
  await expect(page.getByTestId('task.title')).toHaveText('And 3+3?');
  expect(posts).toEqual(['/v1/tasks']);
});

test('suggestion chips fill the composer and never send; the GitHub chip offers "Connect GitHub"', async ({
  page,
  baseURL,
}) => {
  await freshUser(page, baseURL!);
  let tasks = 0;
  page.on('request', (request) => {
    if (request.url().endsWith('/v1/tasks')) tasks += 1;
  });
  const prompt = page.getByTestId('composer.prompt');
  await page.getByTestId('composer.chip.research').click();
  await expect(prompt).toHaveValue('Summarize the current state of ');
  await expect(prompt).toBeFocused();
  await expect(page.getByTestId('composer.github-hint')).toHaveCount(0);
  await page.getByTestId('composer.chip.github').click();
  await expect(prompt).toHaveValue('Open a pull request that ');
  await expect(page.getByTestId('composer.github-connect')).toHaveText('Connect GitHub');
  await page.getByTestId('composer.chip.explain').click();
  await expect(prompt).toHaveValue('Explain this repository: ');
  await page.getByTestId('composer.chip.plan').click();
  await expect(prompt).toHaveValue('Write a short plan for ');
  await expect(page.getByTestId('composer.github-hint')).toHaveCount(0);
  await prompt.fill('   ');
  await expect(page.getByTestId('composer.send')).toBeDisabled();
  expect(tasks).toBe(0);
  await page.getByTestId('composer.chip.github').click();
  await page.getByTestId('composer.github-connect').click();
  await expect(page).toHaveURL(/\/settings$/);
});

test('the workgroup choice lists only writable workgroups and the task lands in the chosen one', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  const research = await api.createWorkgroup(user.org, 'Research');
  await api.createWorkgroup(user.org, 'Archive readers', 'read');
  // the `log` frame brings the new workgroups (log and wrap jobs) without a reload
  const select = page.getByTestId('home.wg');
  await expect(select.locator('option')).toHaveText(['Default', 'Research']);
  await select.selectOption(research);
  const task = await sendPrompt(page, 'A research task');
  const session = await mainSession(task);
  expect(session.device_id).toBe(user.device);
  const row = (await api.request('GET', `/v1/tasks/${task}`)) as { task: { wg_id: string } };
  expect(row.task.wg_id).toBe(research);
});

test('the composer says "Offline" without network and sends nothing', async ({ page, context, baseURL }) => {
  await freshUser(page, baseURL!);
  await page.getByTestId('composer.prompt').fill('Offline question');
  await context.setOffline(true);
  await expect(page.getByTestId('composer.send')).toHaveText('Offline');
  await expect(page.getByTestId('composer.send')).toBeDisabled();
  await context.setOffline(false);
  await expect(page.getByTestId('composer.send')).toHaveText('Send');
});

test('while the membership log is not verified, home and task list say so instead of guessing rights', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  await page.route(`**/v1/orgs/${user.org}/log**`, (route) =>
    route.fulfill({ status: 503, json: { error: 'unavailable' } }),
  );
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('home.loading')).toBeVisible();
  await expect(page.getByTestId('composer')).toHaveCount(0);
  await page.goto(`/o/${user.org}/w/${user.org}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('tasks.loading')).toBeVisible();
});
