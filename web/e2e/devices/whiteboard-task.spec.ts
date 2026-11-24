// The task-level whiteboard (06-web.md section 6.1, tab "Whiteboard" of the task page) against the real hub with the
// device double (a task needs a device, so this spec lives in the `devices` project): the owner writes, edits and
// reads the history of the task's board; entries of the task stay off the workgroup board. (Task notifications come
// from hub signals that the device double's process does not run; their texts are covered by Vitest.)
import { expect, test } from '../fixtures/test.ts';
import { freshUser, sendPrompt } from '../helpers/tasks.ts';
import { navigateInApp } from '../helpers/whiteboard.ts';
import { smallTargets } from '../helpers/layout-checks.ts';

test('the task board and its history @R-12.1 @R-12.3 @R-12.7', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Plan the release');
  await page.getByTestId('task.tabs.whiteboard').click();
  await expect(page).toHaveURL(/\?tab=whiteboard$/);
  await expect(page.getByTestId('whiteboard.board')).toHaveAttribute('data-level', 'task');
  await expect(page.getByTestId('whiteboard.empty')).toBeVisible();
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill('Checklist\n- build\n- test');
  await page.getByTestId('whiteboard.editor.save').click();
  const entry = page.getByTestId('whiteboard.item').filter({ hasText: 'Checklist' });
  await expect(entry.locator('li')).toHaveText(['build', 'test']);
  // the entry's controls are touch targets of at least 44 × 44 px (06-web.md section 12.1; found by E2E-29)
  expect(await smallTargets(page)).toEqual([]);
  await entry.getByTestId('whiteboard.item.edit').click();
  await page.getByTestId('whiteboard.editor.text').fill('Checklist\n- build\n- test\n- release');
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(entry.locator('li')).toHaveCount(3);
  await entry.getByTestId('whiteboard.item.history').click();
  // newest first: the edit, then the creation; its diff once expanded
  const versions = page.getByTestId('whiteboard.history.version');
  await expect(page.getByTestId('whiteboard.history.action')).toHaveText(['Edited', 'Created']);
  const last = versions.nth(0);
  await last.getByTestId('whiteboard.history.diff.toggle').click();
  await expect(last.getByTestId('whiteboard.diff.stats')).toHaveText('+1 / −0 lines');
  await expect(last.locator('[data-op="add"]')).toHaveText('+ - release');
  await page.getByTestId('whiteboard.history.close').click();
  // the workgroup board does not show task entries
  await navigateInApp(page, `/o/${user.org}/whiteboard`);
  await page.getByTestId('whiteboard.level').nth(1).click();
  await expect(page).toHaveURL(/\/w\/[0-9a-f-]{36}\/whiteboard$/);
  await expect(page.getByTestId('whiteboard.empty')).toBeVisible();
  expect(task).toMatch(/^[0-9a-f-]{36}$/);
});
