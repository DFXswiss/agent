// KOR-11 (AUD-02 NA-05): row frames lost while the live channel was down are visible after the reconnect. The hub
// replays events after a reconnect, but no `task`/`session` frames; the task page loads the task again after `hello`.
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

const STREAM = '**/v1/stream**';

test('a session state sent while the channel was down shows after the reconnect', async ({ page, baseURL }) => {
  // the channel cannot connect: every stream ends at once, so no frame of the transitions below arrives
  await page.route(STREAM, (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: ': hi\n\n' }),
  );
  await freshUser(page, baseURL!, { poolReady: 0 });
  const task = await sendPrompt(page, 'What is 2+2?');
  const state = page.getByTestId('task.state');
  await expect(state).toHaveAttribute('data-state', 'starting');
  await expect(page.getByTestId('shell.connection.reconnecting')).toBeVisible();
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(state).toHaveAttribute('data-state', 'starting');
  // the hub is reachable again: the reconnect's `hello` makes the page load the task with its sessions
  await page.unroute(STREAM);
  await expect(page.getByTestId('shell.connection.reconnecting')).toHaveCount(0, { timeout: 40_000 });
  await expect(state).toHaveAttribute('data-state', 'working');
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
});
