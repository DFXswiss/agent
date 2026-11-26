// Captured tasks (E-41, ADR-100) against the real hub, on the desktop and on the phones: "Capture task" on the task
// overview and on a workgroup's task list (title, description, person, a topic created inline, GitHub organisation
// and repository, urgency) creates a task without an agent; the rows show status, urgency, person and topic; the
// filters (status, urgency, person and topic at the hub, GitHub organisation and repository in the browser); the task
// page shows the fields, edits them, changes the status and starts the agent with the first prompt.
import { expect, type Page } from '@playwright/test';
import { test } from '../fixtures/test.ts';
import { mainSession, TASK_PATH } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { freshUser } from '../helpers/tasks.ts';

/** Opens the overview the way a user does: the bottom bar on a phone, else the main navigation. */
async function openTasks(page: Page): Promise<void> {
  const bottom = page.getByTestId('shell.bottom.tasks');
  if (await bottom.isVisible()) await bottom.click();
  else await page.getByTestId('shell.nav.tasks').click();
  await expect(page.getByTestId('tasks.overview')).toBeVisible();
}

interface Ticket {
  readonly title: string;
  readonly description?: string;
  readonly person?: string;
  /** An existing topic's name, or `{ create }` a new one. */
  readonly topic?: string | { readonly create: string };
  readonly org?: string;
  readonly repo?: string;
  readonly urgency?: string;
}

/** Fills and sends "Capture task" on the current page; returns the new task's ID. */
async function capture(page: Page, ticket: Ticket): Promise<string> {
  await page.getByTestId('tickets.capture.open').click();
  const form = page.getByTestId('tickets.capture');
  await form.getByTestId('tickets.capture.title').fill(ticket.title);
  if (ticket.description !== undefined) await form.getByTestId('tickets.capture.description').fill(ticket.description);
  if (ticket.person !== undefined)
    await form.getByTestId('tickets.capture.person').selectOption({ label: ticket.person });
  if (typeof ticket.topic === 'string') {
    await form.getByTestId('tickets.capture.topic').selectOption({ label: ticket.topic });
  } else if (ticket.topic !== undefined) {
    await form.getByTestId('tickets.capture.topic').selectOption({ label: 'New topic …' });
    await form.getByTestId('tickets.capture.topic.name').fill(ticket.topic.create);
    await form.getByTestId('tickets.capture.topic.create').click();
    await expect(form.getByTestId('tickets.capture.topic.name')).toHaveCount(0);
    await expect(form.getByTestId('tickets.capture.topic').locator('option:checked')).toHaveText(ticket.topic.create);
  }
  if (ticket.repo !== undefined) await form.getByTestId('tickets.capture.github-repo').fill(ticket.repo);
  if (ticket.org !== undefined) await form.getByTestId('tickets.capture.github-org').fill(ticket.org);
  if (ticket.urgency !== undefined) await form.getByTestId('tickets.capture.urgency').selectOption(ticket.urgency);
  await form.getByTestId('tickets.capture.submit').click();
  await expect(page.getByTestId('tickets.capture.done')).toBeVisible();
  const href = (await page.getByTestId('tickets.capture.show').getAttribute('href'))!;
  return TASK_PATH.exec(href)![1]!;
}

test('captures tasks without an agent, filters them, edits one and starts its agent', async ({ page, baseURL }) => {
  test.setTimeout(150_000);
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const requested: string[] = [];
  page.on('request', (request) => requested.push(request.url()));
  await openTasks(page);

  // capture on the overview: nothing starts, the rows show the fields
  const invoices = await capture(page, {
    title: 'Review the invoices',
    description: 'Check the totals of October.',
    person: 'Ada Lovelace',
    topic: { create: 'Billing' },
    repo: 'DFXswiss/agent',
    urgency: 'high',
  });
  const launch = await capture(page, { title: 'Plan the launch', org: 'acme', urgency: 'low' });
  const rows = page.getByTestId('tasks.row');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Plan the launch', 'Review the invoices']);
  const invoiceRow = rows.filter({ hasText: 'Review the invoices' });
  await expect(invoiceRow.getByTestId('tasks.row.status')).toHaveText('To do');
  await expect(invoiceRow.getByTestId('tasks.row.urgency')).toHaveText('Urgency: High');
  await expect(invoiceRow.getByTestId('tasks.row.person')).toHaveText('Person: Ada Lovelace');
  await expect(invoiceRow.getByTestId('tasks.row.topic')).toHaveText('Topic: Billing');
  await expect(invoiceRow.getByTestId('tasks.row.state')).toHaveText('Idle');
  await expect(rows.filter({ hasText: 'Plan the launch' }).getByTestId('tasks.row.topic')).toHaveCount(0);
  // the form, the filters and the rows fit the screen with touch targets of 44 px (06-web.md section 12)
  await page.getByTestId('tickets.capture.open').click();
  expect(await layoutProblems(page)).toEqual([]);
  await page.getByTestId('tickets.capture.cancel').click();

  // the hub filters (status, urgency, person, topic) and the sealed ones in the browser (organisation, repository)
  await page.getByTestId('tickets.filter.urgency').selectOption('high');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Review the invoices']);
  expect(requested.some((url) => url.includes('/tasks?') && url.includes('urgency=high'))).toBe(true);
  await page.getByTestId('tickets.filter.urgency').selectOption('');
  await page.getByTestId('tickets.filter.topic').selectOption({ label: 'No topic' });
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Plan the launch']);
  await page.getByTestId('tickets.filter.topic').selectOption({ label: 'Billing' });
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Review the invoices']);
  await page.getByTestId('tickets.filter.topic').selectOption('');
  await page.getByTestId('tickets.filter.assignee').selectOption({ label: 'Nobody' });
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Plan the launch']);
  await page.getByTestId('tickets.filter.assignee').selectOption('');
  await page.getByTestId('tickets.filter.status').selectOption('completed');
  await expect(page.getByTestId('tasks.empty')).toContainText('No task matches the filters.');
  await page.getByTestId('tickets.filter.status').selectOption('todo');
  await expect(rows).toHaveCount(2);
  // the repository filled the organisation with its owner
  await page.getByTestId('tickets.filter.github-org').fill('dfxswiss');
  await expect(rows.getByTestId('tasks.row.title')).toHaveText(['Review the invoices']);
  await page.getByTestId('tickets.filter.github-org').fill('');
  await page.getByTestId('tickets.filter.github-repo').fill('acme/web');
  await expect(page.getByTestId('tasks.empty')).toBeVisible();
  await page.getByTestId('tasks.filter.reset').click();
  await expect(rows).toHaveCount(2);

  // the task page: the fields, "Edit", the status choice and "Start agent"
  await rows.filter({ hasText: 'Review the invoices' }).click();
  await expect(page).toHaveURL(new RegExp(`/t/${invoices}$`));
  const fields = page.getByTestId('task.fields');
  await expect(fields.getByTestId('task.fields.status')).toHaveValue('todo');
  await expect(fields.getByTestId('task.fields.person')).toHaveText('Ada Lovelace');
  await expect(fields.getByTestId('task.fields.topic')).toHaveText('Billing');
  await expect(fields.getByTestId('task.fields.github-org')).toHaveText('DFXswiss');
  await expect(fields.getByTestId('task.fields.github-repo')).toHaveText('DFXswiss/agent');
  await expect(fields.getByTestId('task.fields.description')).toHaveText('Check the totals of October.');
  await fields.getByTestId('task.fields.edit').click();
  expect(await layoutProblems(page)).toEqual([]);
  await fields.getByTestId('task.fields.form.title').fill('Review the invoices of October');
  await fields.getByTestId('task.fields.form.urgency').selectOption('urgent');
  await fields.getByTestId('task.fields.form.person').selectOption('');
  await fields.getByTestId('task.fields.save').click();
  await expect(fields.getByTestId('task.fields.form')).toHaveCount(0);
  await expect(page.getByTestId('task.title')).toHaveText('Review the invoices of October');
  await expect(fields.getByTestId('task.fields.urgency')).toHaveText('Urgency: Urgent');
  await expect(fields.getByTestId('task.fields.person')).toHaveText('Nobody');
  await expect(fields.getByTestId('task.fields.github-repo')).toHaveText('DFXswiss/agent');
  await fields.getByTestId('task.fields.status').selectOption('inProgress');
  await expect(fields.getByTestId('task.fields.status')).toHaveValue('inProgress');

  await fields.getByTestId('task.start.open').click();
  await expect(fields.getByTestId('task.start.prompt')).toHaveValue(
    'Review the invoices of October\n\nCheck the totals of October.',
  );
  await fields.getByTestId('task.start.prompt').fill('Review the invoices of October and list every gap.');
  await fields.getByTestId('task.start.submit').click();
  await expect(fields.getByTestId('task.start')).toHaveCount(0);
  const session = await mainSession(invoices);
  expect(session.device_id).toBe(user.device);
  await expect(fields.getByTestId('task.fields.status')).toHaveValue('inProgress');

  // completed closes the task
  await fields.getByTestId('task.fields.status').selectOption('completed');
  await expect(page.getByTestId('task.state')).toHaveAttribute('data-state', 'closed');

  // the workgroup's task list captures too and shows the same fields
  await openTasks(page);
  const launchRow = rows.filter({ hasText: 'Plan the launch' });
  await expect(launchRow).toHaveAttribute('data-task', launch);
  const wg = (await launchRow.getByTestId('tasks.row.wg').getAttribute('data-wg'))!;
  await launchRow.click();
  await expect(page.getByTestId('task.start')).toBeVisible();
  await expect(page.getByTestId('task.fields.github-org')).toHaveText('acme');
  // the workgroup's list is a link of its own: a reload unlocks again with the passkey
  await page.goto(`/o/${user.org}/w/${wg}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('tasks.screen')).toBeVisible();
  await capture(page, { title: 'Write the release notes', topic: 'Billing' });
  const listRows = page.getByTestId('tasks.row');
  await expect(listRows.filter({ hasText: 'Write the release notes' }).getByTestId('tasks.row.topic')).toHaveText(
    'Topic: Billing',
  );
  await page.getByTestId('tickets.filter.status').selectOption('completed');
  await expect(listRows.getByTestId('tasks.row.title')).toHaveText(['Review the invoices of October']);
});
