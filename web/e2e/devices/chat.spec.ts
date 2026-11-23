// The chat of a task (06-web.md section 5.2) against the real hub with device-authored session events (device double):
// every base item (prompt, Markdown output with safe links, tool row, file change with patch, blocked hook, session
// status, countdown summary, transitions), the composer with follow-up, Stop, End session and Close task for writers,
// and the read-only view for readers.
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { grantOwnAccess, sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

const TIME = /^\d{2}:\d{2}:\d{2}\.\d{3}$/;

const OUTPUT =
  '# Overview\n\nSee [the docs](https://example.com/docs) and [bad](javascript:alert(1)).\n\n' +
  '<b>raw html</b>\n\n![logo](https://example.com/logo.png)\n\n- one\n- two\n\n`code`';

test('shows every base item of the timeline in order', async ({ page, baseURL }) => {
  await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Explain the repository');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
  await sessionEvents(session.session_id, [
    { kind: 'session.output', body: { message_id: 'm1', role: 'assistant', turn: 1, text: OUTPUT } },
    {
      kind: 'session.tool',
      body: {
        tool_use_id: 't1',
        name: 'Read',
        input_summary: 'src/app.py',
        input: '{"file_path":"src/app.py"}',
        result_summary: 'print("hi")',
        is_error: false,
        duration_ms: 1200,
      },
    },
    {
      kind: 'session.file',
      body: { path: 'src/app.py', op: 'edit', sha256: 'a'.repeat(64), size: 12, patch: '-old\n+new' },
    },
    { kind: 'session.hook', body: { event: 'PreToolUse', tool: 'Bash', decision: 'allow' } },
    { kind: 'session.hook', body: { event: 'PreToolUse', tool: 'Bash', decision: 'deny', reason: 'network' } },
    { kind: 'session.status', body: { state: 'failed', reason: 'oom' } },
  ]);
  const items = page.getByTestId('chat.item');
  // the allowed hook is not shown
  await expect(items).toHaveCount(7);
  expect(await items.evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-kind')))).toEqual([
    'prompt',
    'countdown',
    'session.output',
    'session.tool',
    'session.file',
    'session.hook',
    'session.status',
  ]);
  await expect(page.getByTestId('chat.prompt.text')).toHaveText('Explain the repository');
  await expect(page.getByTestId('chat.prompt.author')).toHaveText('You');
  // Markdown: safe link kept with rel, javascript link and raw HTML dropped, the image only as a link
  const markdown = page.getByTestId('markdown');
  await expect(markdown.locator('h1')).toHaveText('Overview');
  const docs = markdown.getByRole('link', { name: 'the docs' });
  await expect(docs).toHaveAttribute('href', 'https://example.com/docs');
  await expect(docs).toHaveAttribute('rel', 'noopener noreferrer nofollow');
  await expect(markdown.getByTestId('markdown.link-removed')).toHaveText('bad');
  await expect(markdown.locator('b, img, script')).toHaveCount(0);
  await expect(markdown.getByRole('link', { name: 'logo' })).toHaveAttribute('href', 'https://example.com/logo.png');
  await expect(markdown.locator('li')).toHaveText(['one', 'two']);
  // tool row: collapsed, expands to input and result
  const tool = page.getByTestId('chat.tool');
  await expect(tool.getByTestId('chat.tool.summary')).toHaveText('Read src/app.py');
  await expect(tool.getByTestId('chat.tool.input')).toBeHidden();
  await tool.locator('summary').click();
  await expect(tool.getByTestId('chat.tool.input')).toHaveText('{"file_path":"src/app.py"}');
  await expect(tool.getByTestId('chat.tool.result')).toHaveText('print("hi")');
  await expect(tool).toContainText('1.2 s');
  // file change with its patch
  await expect(page.getByTestId('chat.file.path')).toHaveText('src/app.py');
  await expect(page.getByTestId('chat.file.patch')).toHaveCount(0);
  await page.getByTestId('chat.file.toggle').click();
  await expect(page.getByTestId('chat.file.patch')).toHaveText('-old\n+new');
  await expect(page.getByTestId('chat.hook')).toContainText('Blocked: Bash (network)');
  await expect(page.getByTestId('chat.status')).toHaveAttribute('data-state', 'failed');
  await expect(page.getByTestId('chat.item.time').first()).toHaveText(TIME);
  // a transition becomes a system row
  await walk(session.session_id, ['idle', 'suspended']);
  await expect(page.locator('[data-testid="chat.status"][data-source="transition"]')).toHaveText(
    /^Session suspended \d{2}:\d{2}:\d{2}\.\d{3}$/,
  );
});

test('a writer sends follow-ups, stops and ends the session and closes the task', async ({ page, baseURL }) => {
  await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Long running work');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('chat.control.stop')).toBeVisible();
  const posted = (path: string) =>
    page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname === path);
  // follow-up while working: the session queues it
  const prompt = posted(`/v1/tasks/${task}/prompts`);
  await page.getByTestId('chat.composer.prompt').fill('And now the tests');
  await page.getByTestId('chat.composer.prompt').press('Control+Enter');
  await prompt;
  await expect(page.getByTestId('chat.prompt.text')).toHaveText(['Long running work', 'And now the tests']);
  await expect(page.getByTestId('chat.composer.prompt')).toHaveValue('');
  // Stop = interrupt
  const interrupt = posted(`/v1/sessions/${session.session_id}/control`);
  await page.getByTestId('chat.control.stop').click();
  expect((await interrupt).postDataJSON()).toEqual({ action: 'interrupt' });
  // End session = stop, from the session menu
  await page.getByTestId('chat.control.menu').locator('summary').click();
  const stop = posted(`/v1/sessions/${session.session_id}/control`);
  const stopped = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/${session.session_id}/control`) &&
      (response.request().postDataJSON() as { action?: string } | null)?.action === 'stop',
  );
  await page.getByTestId('chat.control.end').click();
  expect((await stop).postDataJSON()).toEqual({ action: 'stop' });
  // the device ends the session only after the hub took the stop: a session the device ends by itself with a queued
  // prompt is followed by a new main session for it (KOR-11, NA-04), one the user stopped is not
  expect((await stopped).status()).toBe(200);
  await walk(session.session_id, ['stopping', 'stopped']);
  await expect(page.getByTestId('chat.control.stop')).toHaveCount(0);
  await expect(page.getByTestId('chat.control.menu')).toHaveCount(0);
  await expect(page.locator('[data-testid="chat.status"][data-state="stopped"]')).toBeVisible();
  // Close task, with a confirmation that can be cancelled
  await page.getByTestId('task.close').click();
  await page.getByTestId('task.close.cancel').click();
  await page.getByTestId('task.close').click();
  const close = posted(`/v1/tasks/${task}/close`);
  await page.getByTestId('task.close.confirm').click();
  await close;
  await expect(page.getByTestId('task.state')).toHaveAttribute('data-state', 'closed');
  await expect(page.getByTestId('task.close')).toHaveCount(0);
  await expect(page.locator('[data-testid="chat.status"][data-state="closed"]')).toBeVisible();
});

test('a reader sees the chat with "You have read access" and no controls', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  // a second workgroup (the default one is always writable), a task in it, then the admin keeps only read access
  const api = await PageUser.open(page, user.recoveryCode);
  const wg = await api.createWorkgroup(user.org, 'Team');
  await page.reload();
  await page.getByTestId('login.submit').click();
  await page.getByTestId('home.wg').selectOption(wg);
  const task = await sendPrompt(page, 'Visible to readers');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('chat.control.stop')).toBeVisible();
  await expect(page.getByTestId('task.close')).toBeVisible();
  await grantOwnAccess(api, user.org, wg, 'read');
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('chat.prompt.text')).toHaveText('Visible to readers');
  await expect(page.getByTestId('chat.read-only')).toHaveText('You have read access');
  await expect(page.getByTestId('chat.composer')).toHaveCount(0);
  await expect(page.getByTestId('chat.control.stop')).toHaveCount(0);
  await expect(page.getByTestId('task.menu')).toHaveCount(0);
});
