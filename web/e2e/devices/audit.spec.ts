// The task tab "Audit" (06-web.md sections 2.4 and 9.2) against the real hub and the device double: the rows of the
// task only (the task filter is fixed and not shown), each linked to the task, paged with "Load more", and the link to
// a referenced event (the prompt on the task page, a session event on the session detail).
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

test('the task tab shows the task rows and links the referenced events @R-7.6', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Audit this task');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  // more rows than one page (100): every stored event is audited
  await sessionEvents(
    main.session_id,
    Array.from({ length: 110 }, (_, i) => ({
      kind: 'session.output',
      body: { message_id: `m${i}`, role: 'assistant', turn: 1, text: `Step ${i}` },
    })),
  );
  await page.getByTestId('task.tabs.audit').click();
  await expect(page).toHaveURL(/\?tab=audit$/);
  const rows = page.getByTestId('audit.row');
  await expect(rows).toHaveCount(100);
  await page.getByTestId('audit.more').click();
  await expect.poll(() => rows.count()).toBeGreaterThan(110);
  await expect(page.getByTestId('audit.filter.task')).toHaveCount(0);
  const links = await rows
    .getByTestId('audit.row.task')
    .evaluateAll((items) => items.map((a) => a.getAttribute('href')));
  expect(links.length).toBeGreaterThan(0);
  expect(new Set(links)).toEqual(new Set([`/o/${user.org}/t/${task}`]));
  await expect(rows.getByTestId('audit.row.task').first()).toHaveText('Audit this task');
  // a session event of the task links to the session detail
  const sessionEvent = page.locator(`[data-testid="audit.row.event"][href="/o/${user.org}/s/${main.session_id}"]`);
  await expect(sessionEvent.first()).toBeVisible();
  // filters keep the tab
  await page.getByTestId('audit.filter.action').fill('event');
  await page.getByTestId('audit.filter.apply').click();
  await expect(page).toHaveURL(/\?action=event&tab=audit$/);
  await expect(rows.first()).toHaveAttribute('data-action', 'event.append');
  await rows.first().getByTestId('audit.row.event').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/(t/${task}|s/${main.session_id})$`));
});
