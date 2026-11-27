// The overview of the open tasks at the top of the start page (contracts/changes/HOME-COUNTS.md) against the real hub
// and the device double, on the desktop and on the phones (`MOBILE_DEVICE_SPECS`): right after the sign-in it is the
// first thing of the page; "In progress", "Waiting for a question" and "No agent" count every open task exactly once
// (the hub reads them from the attention records); a number shows exactly its tasks; the numbers and the list follow
// the live channel without a reload.
import { expect, type Page } from '@playwright/test';
import { test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { operation } from '../helpers/operations.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

/** The task's main session, started and working (as the runner reports it). */
async function working(task: string): Promise<string> {
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working']);
  await expect.poll(async () => (await mainSession(task)).state, { timeout: 20_000 }).toBe('working');
  return main.session_id;
}

const count = (page: Page, name: string) => page.getByTestId(`overview.count.${name}`);

async function counts(page: Page, working: number, waiting: number, noAgent: number): Promise<void> {
  await expect(count(page, 'working')).toHaveAttribute('data-count', String(working), { timeout: 20_000 });
  await expect(count(page, 'waiting')).toHaveAttribute('data-count', String(waiting), { timeout: 20_000 });
  await expect(count(page, 'no_agent')).toHaveAttribute('data-count', String(noAgent), { timeout: 20_000 });
}

const titles = (page: Page, list: string) =>
  page.getByTestId(`attention.group.${list}`).getByTestId('attention.item').getByTestId('tasks.row.title');

test('the start page counts the open tasks in three numbers, lists each and follows them live', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(180_000);
  const user = await freshUser(page, baseURL!);
  // the first task from the composer (it places the workgroup on the new platform device), the others by the API
  const asking = await working(await sendPrompt(page, 'Ask about the release'));
  const api = await PageUser.open(page, user.recoveryCode);
  const wg = (await api.log(user.org)).state.defaultWg;
  // one task after the other, each created once the one before ended its turn (the workgroup's VMs are few)
  const task = async (title: string) => (await api.createTasks(user.org, wg, [title]))[0]!;
  await operation(asking, 'human.question', { question: 'Which release?' }, { state: 'succeeded', result: {} });
  await walk(asking, ['idle']);
  await walk(await working(await task('Crash on start')), ['failed'], { reason: 'agent_exited' });
  await walk(await working(await task('Rest after the work')), ['idle']);
  const done = await task('Close the finished task');
  await walk(await working(done), ['idle']);
  // a closed task is not open: it counts nowhere
  await api.request('POST', `/v1/tasks/${done}/close`, {});
  const running = await working(await task('Run the nightly build'));

  // after the sign-in: the overview is the first thing of the start page
  await page.goto('/');
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}$`));
  const overview = page.getByTestId('overview');
  await expect(overview).toBeVisible();
  expect(
    await page.getByTestId('home.screen').evaluate((node) => node.firstElementChild?.getAttribute('data-testid')),
  ).toBe('overview');
  await counts(page, 1, 1, 2);
  expect(await layoutProblems(page)).toEqual([]);

  // a number shows exactly its tasks
  await count(page, 'no_agent').click();
  await expect(page).toHaveURL(/\?show=no_agent$/);
  await expect(titles(page, 'no_agent')).toHaveText(['Rest after the work', 'Crash on start']);
  await expect(page.getByTestId('attention.group.blocked')).toHaveCount(0);
  expect(await layoutProblems(page)).toEqual([]);
  await count(page, 'waiting').click();
  await expect(titles(page, 'needs_decision')).toHaveText(['Ask about the release']);
  await count(page, 'working').click();
  await expect(titles(page, 'working')).toHaveText(['Run the nightly build']);

  // live: the running task ends its turn and lies idle – it moves from "In progress" to "No agent" without a reload
  await walk(running, ['idle']);
  await counts(page, 0, 1, 3);
  await expect(titles(page, 'working')).toHaveCount(0);
  await count(page, 'no_agent').click();
  await expect(titles(page, 'no_agent')).toHaveText(['Run the nightly build', 'Rest after the work', 'Crash on start']);
  await page.getByTestId('overview.all').click();
  await expect(page.getByTestId('attention.group.needs_decision')).toBeVisible();
});
