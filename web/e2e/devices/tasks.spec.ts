// Task list of a workgroup (06-web.md section 5.1) against the real hub: newest first with decrypted titles, state,
// owner and last activity; the state filter; endless scrolling over more than one page; a reader sees no composer;
// an unknown workgroup is "not found".
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

test('lists the tasks newest first with title, state, owner and activity, filters by state', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const first = await sendPrompt(page, 'First question\nwith a second line');
  const session = await mainSession(first);
  await walk(session.session_id, [...START_STEPS, 'working']);
  await page.getByTestId('shell.nav.home').click();
  await sendPrompt(page, 'Second question');
  await page.getByTestId('shell.nav.home').click();
  await page.getByTestId('home.all-tasks').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/w/[0-9a-f-]{36}$`));
  await expect(page.getByTestId('tasks.title')).toHaveText('Default');
  await expect(page.getByTestId('composer')).toBeVisible();
  const rows = page.getByTestId('tasks.row');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Second question', 'First question']);
  await expect(rows.getByTestId('tasks.row.state')).toHaveText(['Starting', 'Working']);
  await expect(rows.getByTestId('tasks.row.owner')).toHaveText(['Ada Lovelace', 'Ada Lovelace']);
  await expect(rows.first().getByTestId('tasks.row.activity')).toHaveText(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  await page.getByTestId('tasks.filter').selectOption('working');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['First question']);
  await page.getByTestId('tasks.filter').selectOption('closed');
  await expect(page.getByTestId('tasks.empty')).toBeVisible();
  await page.getByTestId('tasks.filter').selectOption('');
  await rows.first().click();
  await expect(page.getByTestId('task.title')).toHaveText('Second question');
});

test('loads the next page when the end of the list becomes visible', async ({ page, baseURL }) => {
  test.setTimeout(90_000);
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  await sendPrompt(page, 'Task 0');
  const api = await PageUser.open(page, user.recoveryCode);
  const wg = ((await api.request('GET', `/v1/orgs/${user.org}`)) as { default_wg: string }).default_wg;
  await api.createTasks(
    user.org,
    wg,
    Array.from({ length: 55 }, (_, index) => `Task ${index + 1}`),
  );
  await page.goto(`/o/${user.org}/w/${wg}`);
  await page.getByTestId('login.submit').click();
  const rows = page.getByTestId('tasks.row');
  await expect(rows).toHaveCount(50);
  await expect(rows.first().getByTestId('tasks.row.title')).toHaveText('Task 55');
  await page.getByTestId('tasks.end').scrollIntoViewIfNeeded();
  await expect(rows).toHaveCount(56);
  await expect(rows.last().getByTestId('tasks.row.title')).toHaveText('Task 0');
  await expect(page.getByTestId('tasks.more')).toHaveCount(0);
});

test('a reader of a workgroup sees its tasks but no composer; an unknown workgroup is not found', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  const readOnly = await api.createWorkgroup(user.org, 'Readers', 'read');
  await page.goto(`/o/${user.org}/w/${readOnly}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('tasks.title')).toHaveText('Readers');
  await expect(page.getByTestId('tasks.empty')).toBeVisible();
  await expect(page.getByTestId('composer')).toHaveCount(0);
  await page.goto(`/o/${user.org}/w/0190f000-0000-7000-8000-000000000000`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('tasks.not-found')).toBeVisible();
});
