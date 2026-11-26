// The task dashboard (E-43, ADR-101) against the real hub, on the desktop and on the phones: one click from Home and
// from the menu; the tasks of two organizations of the user in one list with "organization · workgroup"; the counters
// per status and per urgency on top (a click filters, a second click clears; the hub counts); the filters by
// organization, person (Zuständig), urgency (Prio) and status; the order "most urgent first" and the title search.
import { expect, type Page } from '@playwright/test';
import { test } from '../fixtures/test.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { freshUser } from '../helpers/tasks.ts';

/** Opens the dashboard from the menu: the bottom bar on a phone, else the main navigation. */
async function openTasks(page: Page): Promise<void> {
  const bottom = page.getByTestId('shell.bottom.tasks');
  if (await bottom.isVisible()) await bottom.click();
  else await page.getByTestId('shell.nav.tasks').click();
  await expect(page.getByTestId('tasks.overview')).toBeVisible();
}

/** Captures a task without an agent on the dashboard (E-41). */
async function capture(page: Page, title: string, fields: { person?: string; urgency?: string } = {}): Promise<void> {
  await page.getByTestId('tickets.capture.open').click();
  const form = page.getByTestId('tickets.capture');
  await form.getByTestId('tickets.capture.title').fill(title);
  if (fields.person !== undefined) {
    await form.getByTestId('tickets.capture.person').selectOption({ label: fields.person });
  }
  if (fields.urgency !== undefined) await form.getByTestId('tickets.capture.urgency').selectOption(fields.urgency);
  await form.getByTestId('tickets.capture.submit').click();
  await expect(page.getByTestId('tickets.capture.done')).toBeVisible();
}

const count = (page: Page, id: string) => page.getByTestId(`dashboard.count.${id}`);

test('the dashboard lists, counts, filters and sorts the tasks of every organization', async ({ page, baseURL }) => {
  test.setTimeout(150_000);
  const user = await freshUser(page, baseURL!);

  // one click from Home
  await expect(page.getByTestId('home.dashboard')).toBeVisible();
  await page.getByTestId('home.dashboard').click();
  await expect(page.getByTestId('tasks.overview')).toBeVisible();
  // a refused capture says why (E-41): a field that is no GitHub login, then a hub that cannot be reached
  await page.getByTestId('tickets.capture.open').click();
  const form = page.getByTestId('tickets.capture');
  await form.getByTestId('tickets.capture.title').fill('Not captured');
  await form.getByTestId('tickets.capture.github-org').fill('-bad');
  await form.getByTestId('tickets.capture.submit').click();
  await expect(form.getByTestId('tickets.capture.error')).toHaveAttribute('data-code', 'request_invalid');
  await form.getByTestId('tickets.capture.github-org').fill('');
  await page.route('**/v1/tasks', (route) => route.abort());
  await form.getByTestId('tickets.capture.submit').click();
  await expect(form.getByTestId('tickets.capture.error')).not.toHaveAttribute('data-code', 'request_invalid');
  await page.unroute('**/v1/tasks');
  await form.getByTestId('tickets.capture.cancel').click();
  await capture(page, 'Fix the payments', { urgency: 'urgent', person: 'Ada Lovelace' });
  await capture(page, 'Weekly report');

  // a second organization with a task of its own
  await page.getByTestId('org-switcher.current').click();
  await page.getByTestId('org-switcher.create.open').click();
  await page.getByTestId('org-switcher.create.name').fill('Acme AG');
  await page.getByTestId('org-switcher.create.confirm').click();
  await expect(page.getByTestId('org-switcher.current')).toHaveText('Acme AG');
  const acme = /\/o\/([0-9a-f-]{36})$/.exec(page.url())![1]!;
  await openTasks(page);
  await capture(page, 'Write the docs', { urgency: 'high' });

  // both organizations in one list, newest activity first, with their place
  const rows = page.getByTestId('tasks.row');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Write the docs', 'Weekly report', 'Fix the payments']);
  await expect(rows.first().getByTestId('tasks.row.wg')).toContainText('Acme AG · ');
  // the counters of both organizations
  await expect(count(page, 'status.todo')).toHaveAttribute('data-count', '3');
  await expect(count(page, 'status.inProgress')).toHaveAttribute('data-count', '0');
  await expect(count(page, 'urgency.urgent')).toHaveAttribute('data-count', '1');
  await expect(count(page, 'urgency.high')).toHaveAttribute('data-count', '1');
  await expect(count(page, 'urgency.normal')).toHaveAttribute('data-count', '1');
  await expect(page.getByTestId('dashboard.counts.total')).toHaveText('Tasks: 3');
  // the counters, filters and rows fit the screen with touch targets of 44 px (06-web.md section 12)
  expect(await layoutProblems(page)).toEqual([]);

  // a click on a counter filters, a second click clears
  await count(page, 'urgency.urgent').click();
  await expect(count(page, 'urgency.urgent')).toHaveAttribute('aria-pressed', 'true');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Fix the payments']);
  await expect(page.getByTestId('tickets.filter.urgency')).toHaveValue('urgent');
  // the status counters count under the urgency filter
  await expect(count(page, 'status.todo')).toHaveAttribute('data-count', '1');
  await count(page, 'urgency.urgent').click();
  await expect(rows).toHaveCount(3);
  await count(page, 'status.completed').click();
  await expect(page.getByTestId('tasks.empty')).toContainText('No task matches the filters.');
  await count(page, 'status.completed').click();
  await expect(rows).toHaveCount(3);

  // the person (Zuständig)
  await page.getByTestId('tickets.filter.assignee').selectOption({ label: 'Ada Lovelace' });
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Fix the payments']);
  await expect(count(page, 'status.todo')).toHaveAttribute('data-count', '1');
  await page.getByTestId('tickets.filter.assignee').selectOption('');

  // one organization
  await page.getByTestId('tasks.filter.org').selectOption(acme);
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Write the docs']);
  await expect(page.getByTestId('dashboard.counts.total')).toHaveText('Tasks: 1');
  await page.getByTestId('tasks.filter.org').selectOption(user.org);
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Weekly report', 'Fix the payments']);
  await page.getByTestId('tasks.filter.org').selectOption('');

  // most urgent first, and the title search
  await page.getByTestId('tasks.order').selectOption('urgency');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Fix the payments', 'Write the docs', 'Weekly report']);
  await page.getByTestId('tasks.search').fill('REPORT');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Weekly report']);
  await page.getByTestId('tasks.search').fill('');
  await page.getByTestId('tickets.filter.status').selectOption('todo');
  await expect(rows).toHaveCount(3);

  // a changed status moves the counters (the row frame reaches the dashboard)
  await page.getByTestId('tickets.filter.status').selectOption('');
  await rows.filter({ hasText: 'Weekly report' }).click();
  await page.getByTestId('task.fields.status').selectOption('inProgress');
  await expect(page.getByTestId('task.fields.status')).toHaveValue('inProgress');
  await openTasks(page);
  await expect(count(page, 'status.inProgress')).toHaveAttribute('data-count', '1');
  await expect(count(page, 'status.todo')).toHaveAttribute('data-count', '2');
});
