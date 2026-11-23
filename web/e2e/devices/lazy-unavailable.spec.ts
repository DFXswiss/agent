// ACC-FINAL decision 11: a lazily loaded part whose chunk cannot be fetched (the link to the hub is down at that moment)
// degrades to its plain fallback instead of failing the screen – the chat shows the output as plain text, the session's
// terminal tab says it is unavailable; the screen itself stays usable (no error page).
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

test('a lazy chunk that cannot be fetched leaves its plain fallback, not an error page', async ({ page, baseURL }) => {
  await page.route(/\/assets\/(Markdown|TerminalView)-[\w-]+\.js$/, (route) => route.abort('connectionreset'));
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Explain the plan');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  const markdownFailed = page.waitForEvent('requestfailed', (request) => /\/assets\/Markdown-/.test(request.url()));
  await sessionEvents(main.session_id, [
    {
      kind: 'session.output',
      body: { message_id: 'm1', role: 'assistant', turn: 1, text: 'The **plan** has 2 steps.' },
    },
  ]);
  await markdownFailed; // the chunk was asked for and failed: what follows is the fallback, not the loading state
  const output = page.getByTestId('chat.output');
  await expect(output).toContainText('The **plan** has 2 steps.');
  await expect(output.locator('strong')).toHaveCount(0);
  await expect(page.getByTestId('error.reload')).toHaveCount(0);
  await page.getByTestId('task.tabs.sessions').click();
  await page
    .locator(`[data-testid="sessions.node"][data-session="${main.session_id}"]`)
    .getByTestId('sessions.link')
    .first()
    .click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/s/${main.session_id}$`));
  await expect(page.getByTestId('terminal.chunk-unavailable')).toBeVisible();
  await expect(page.getByTestId('session.title')).toBeVisible();
  await expect(page.getByTestId('error.reload')).toHaveCount(0);
});
