// The queue of a task (06-web.md section 5.2, ADR-083 section 3) against the real hub with the device double: a
// follow-up while the session works waits in the queue with its sender, waits for the device while the device is
// offline, and a withdrawal the device cannot decide (the double announces no `prompt.withdraw`) is refused with a
// readable reason.
import { expect, test } from '../fixtures/test.ts';
import { mainSession, setOnline, walk } from '../helpers/director.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

test('a follow-up waits in the queue and its withdrawal is refused for an outdated device', async ({
  page,
  baseURL,
}) => {
  await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Long running work');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('chat.control.stop')).toBeVisible();
  const queued = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === `/v1/tasks/${task}/prompts`,
  );
  await page.getByTestId('chat.composer.prompt').fill('And now the tests');
  await page.getByTestId('chat.composer.prompt').press('Control+Enter');
  expect((await queued).ok()).toBe(true);
  const queue = page.getByTestId('chat.queue');
  await expect(queue).toBeVisible();
  await expect(page.getByTestId('chat.queue.heading')).toHaveText(/^\d+ instructions? waiting$/);
  const item = page.getByTestId('chat.queue.item').last();
  await expect(item).toHaveAttribute('data-state', 'waiting');
  await expect(item.getByTestId('chat.queue.author')).toHaveText('You');
  // the device goes offline: the queue says so (read again when the page opens)
  await setOnline(session.device_id!, false);
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('chat.queue.item').last()).toHaveAttribute('data-state', 'waiting_for_device');
  await setOnline(session.device_id!, true);
  await page.reload();
  await page.getByTestId('login.submit').click();
  const back = page.getByTestId('chat.queue.item').last();
  await expect(back).toHaveAttribute('data-state', 'waiting');
  // the double never announced `prompt.withdraw`: the hub refuses, the reason is shown
  await back.getByTestId('chat.queue.withdraw').click();
  const refused = back.getByTestId('chat.queue.error');
  await expect(refused).toHaveAttribute('data-code', 'peer_outdated');
  await expect(refused).toContainText('the device of the session must be updated first');
  await expect(back).toHaveAttribute('data-state', 'waiting');
});
