// Whiteboards on the org and workgroup level (06-web.md section 6.1, KONZEPT section 12) against the real hub: the
// admin writes, edits and deletes on the org board, a member reads without controls and sees every change live in a
// second browser context, the workgroup boards follow wg write/read (a reader of "Research" gets no controls there,
// but writes the default workgroup's board), the history drawer lists all versions with the line diff, and the hub
// stores only ciphertext. The task level runs in `devices/whiteboard-task.spec.ts` (a task needs a device).
import { devices } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { uuid7 } from '../src/core/encoding/ids';
import { boardEvent, navigateInApp, team } from './helpers/whiteboard.ts';

const item = (page: import('@playwright/test').Page, text: string) =>
  page.getByTestId('whiteboard.item').filter({ hasText: text });

test('the admin writes the org board; a member reads it live without controls @R-12.1 @R-12.3 @R-12.4 @R-12.5 @R-12.6 @R-12.7', async ({
  page,
  baseURL,
  newContext,
}) => {
  const { org, ada, bob } = await team(page, newContext, baseURL!);
  await page.getByTestId('shell.nav.whiteboard').click();
  await expect(page).toHaveURL(new RegExp(`/o/${org}/whiteboard$`));
  await expect(page.getByTestId('whiteboard.title')).toHaveText('Organization whiteboard');
  await expect(page.getByTestId('whiteboard.empty')).toBeVisible();
  await bob.page.getByTestId('shell.nav.whiteboard').click();
  await expect(bob.page.getByTestId('whiteboard.readonly')).toBeVisible();
  await expect(bob.page.getByTestId('whiteboard.create')).toHaveCount(0);
  // create (cancel first, then with Markdown and a link)
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.cancel').click();
  await page.getByTestId('whiteboard.create').click();
  await expect(page.getByTestId('whiteboard.editor.save')).toBeDisabled();
  await page
    .getByTestId('whiteboard.editor.text')
    .fill('# Release plan\nShip **Friday**\n[Docs](https://example.com/docs)');
  await page.getByTestId('whiteboard.editor.save').click();
  const adaEntry = item(page, 'Release plan');
  await expect(adaEntry.getByTestId('whiteboard.item.author')).toHaveText('Ada Admin');
  await expect(adaEntry.getByTestId('whiteboard.item.time')).toHaveText(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  await expect(adaEntry.locator('strong')).toHaveText('Friday');
  await expect(adaEntry.locator('a')).toHaveAttribute('rel', 'noopener noreferrer nofollow');
  // the member's browser shows it live, without controls
  const bobEntry = item(bob.page, 'Release plan');
  await expect(bobEntry).toBeVisible();
  await expect(bobEntry.getByTestId('whiteboard.item.edit')).toHaveCount(0);
  await expect(bobEntry.getByTestId('whiteboard.item.delete')).toHaveCount(0);
  // edit: the text changes in both browsers and is marked as edited
  await adaEntry.getByTestId('whiteboard.item.edit').click();
  await page
    .getByTestId('whiteboard.editor.text')
    .fill('# Release plan\nShip **Monday**\n[Docs](https://example.com/docs)');
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(adaEntry.getByTestId('whiteboard.item.edited')).toHaveText('edited');
  await expect(bobEntry.locator('strong')).toHaveText('Monday');
  // history with the line diff, also for the reader
  await bobEntry.getByTestId('whiteboard.item.history').click();
  const versions = bob.page.getByTestId('whiteboard.history.version');
  await expect(versions).toHaveCount(2);
  await expect(versions.nth(0).getByTestId('whiteboard.history.action')).toHaveText('Created');
  await expect(versions.nth(1).getByTestId('whiteboard.history.action')).toHaveText('Edited');
  await expect(versions.nth(1).getByTestId('whiteboard.history.author')).toHaveText('Ada Admin');
  await expect(versions.nth(1).getByTestId('whiteboard.diff.stats')).toHaveText('+1 / −1 lines');
  await expect(versions.nth(1).locator('[data-op="del"]')).toHaveText('− Ship **Friday**');
  await expect(versions.nth(1).locator('[data-op="add"]')).toHaveText('+ Ship **Monday**');
  await bob.page.getByTestId('whiteboard.history.close').click();
  await expect(bob.page.getByTestId('whiteboard.history')).toHaveCount(0);
  // delete: confirmation, then a muted row in both browsers; the history stays
  await adaEntry.getByTestId('whiteboard.item.delete').click();
  await adaEntry.getByTestId('whiteboard.item.delete.cancel').click();
  await adaEntry.getByTestId('whiteboard.item.delete').click();
  await adaEntry.getByTestId('whiteboard.item.delete.confirm').click();
  const deleted = page.locator('[data-testid="whiteboard.item"][data-type="entry"]');
  await expect(deleted.getByTestId('whiteboard.item.deleted')).toBeVisible();
  await expect(deleted.getByTestId('whiteboard.item.edit')).toHaveCount(0);
  await expect(bob.page.getByTestId('whiteboard.item.deleted')).toBeVisible();
  await deleted.getByTestId('whiteboard.item.history').click();
  await expect(page.getByTestId('whiteboard.history.version')).toHaveCount(3);
  await expect(page.getByTestId('whiteboard.history.action').nth(2)).toHaveText('Deleted');
  await page.keyboard.press('Escape');
  // the hub holds only ciphertext of the board
  const stored = JSON.stringify(
    await ada.user.request('GET', `/v1/scopes/${org}/events?kinds=whiteboard.entry,whiteboard.edit`),
  );
  expect(stored).toContain('whiteboard.edit');
  expect(stored).not.toContain('Release plan');
  expect(stored).not.toContain('Monday');
  const rows = (await ada.user.request('GET', `/v1/whiteboards?org=${org}&level=org`)) as { entries: unknown[] };
  expect(rows.entries).toHaveLength(1);
});

test('workgroup boards follow wg write and read; no project level @R-12.1 @R-12.2 @R-12.3 @R-12.4', async ({
  page,
  baseURL,
  newContext,
}) => {
  const { org, research, ada, bob } = await team(page, newContext, baseURL!);
  await navigateInApp(page, `/o/${org}/whiteboard`);
  const levels = page.getByTestId('whiteboard.level');
  // organization, the default workgroup first, then Research – and nothing else (no project level)
  await expect(levels).toHaveText(['Organization', 'Default', 'Research']);
  await levels.filter({ hasText: 'Research' }).click();
  await expect(page).toHaveURL(new RegExp(`/o/${org}/w/${research}/whiteboard$`));
  await expect(page.getByTestId('whiteboard.title')).toHaveText('Whiteboard of Research');
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill('Research notes');
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(item(page, 'Research notes')).toBeVisible();
  // Bob reads Research without controls ...
  await navigateInApp(bob.page, `/o/${org}/w/${research}/whiteboard`);
  await expect(item(bob.page, 'Research notes')).toBeVisible();
  await expect(bob.page.getByTestId('whiteboard.create')).toHaveCount(0);
  await expect(bob.page.getByTestId('whiteboard.item.edit')).toHaveCount(0);
  // ... and writes the default workgroup's board, where Ada sees it live
  await bob.page.getByTestId('whiteboard.level').filter({ hasText: 'Default' }).click();
  await bob.page.getByTestId('whiteboard.create').click();
  await bob.page.getByTestId('whiteboard.editor.text').fill('Bob was here');
  await bob.page.getByTestId('whiteboard.editor.save').click();
  await expect(item(bob.page, 'Bob was here').getByTestId('whiteboard.item.edit')).toBeVisible();
  await page.getByTestId('whiteboard.level').filter({ hasText: 'Default' }).click();
  await expect(item(page, 'Bob was here').getByTestId('whiteboard.item.author')).toHaveText('Bob Member');
  // the hub refuses Bob's direct write into Research and onto the org board (rights in the backend too)
  const log = await ada.user.log(org);
  for (const board of [
    { org, wg: research, task: null, level: 'workgroup' },
    { org, wg: null, task: null, level: 'org' },
  ]) {
    const event = await boardEvent(bob.user, log, board, 'whiteboard.entry', uuid7(), {
      level: board.level,
      text: 'x',
    });
    await expect(bob.user.request('POST', '/v1/events', { events: [{ event }] })).rejects.toThrow(
      /400 .*author_not_writer/,
    );
  }
  // a workgroup Bob cannot read is not found
  await navigateInApp(bob.page, `/o/${org}/w/0190f000-0000-7000-8000-00000000abcd/whiteboard`);
  await expect(bob.page.getByTestId('whiteboard.not-found')).toBeVisible();
  // an org this browser never verified stays loading
  await navigateInApp(bob.page, `/o/0190f000-0000-7000-8000-00000000abcd/whiteboard`);
  await expect(bob.page.getByTestId('whiteboard.loading')).toBeVisible();
});

test.describe('on a phone', () => {
  // Pixel 7 without `defaultBrowserType` (a describe group cannot switch the browser; it is Chromium anyway)
  const { defaultBrowserType: _browser, ...pixel7 } = devices['Pixel 7'];
  test.use(pixel7);

  test('the board and its history work at phone size @R-12.1', async ({ page, baseURL, newContext }) => {
    const { org } = await team(page, newContext, baseURL!);
    await navigateInApp(page, `/o/${org}/whiteboard`);
    await page.getByTestId('whiteboard.create').click();
    await page.getByTestId('whiteboard.editor.text').fill('Phone entry');
    await page.getByTestId('whiteboard.editor.save').click();
    await item(page, 'Phone entry').getByTestId('whiteboard.item.history').click();
    await expect(page.getByTestId('whiteboard.history.version')).toHaveCount(1);
    const width = await page.getByTestId('whiteboard.history').evaluate((box) => box.getBoundingClientRect().width);
    expect(width).toBeLessThanOrEqual(page.viewportSize()!.width);
    expect(page.viewportSize()!.width).toBeLessThan(500);
    await page.getByTestId('whiteboard.history.close').click();
  });
});
