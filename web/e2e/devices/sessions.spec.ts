// The Sessions tab (06-web.md section 5.3) and the session detail (section 5.5) against the real hub: the tree of main
// session and subagents (depth ≤ 2) with state, device and model; a node opens the detail with the tabs Terminal,
// Chat (this session's outputs only, subagent output indented in the task chat) and Children.
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { childSession, sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

const output = (text: string) => ({
  kind: 'session.output',
  body: { message_id: text, role: 'assistant', turn: 1, text },
});

test('shows the session tree and the detail of a subagent', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Split the work');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  const child = await childSession(main.session_id, { model: 'claude-sonnet' });
  const grandchild = await childSession(child, { state: 'idle' });
  await sessionEvents(main.session_id, [output('Main says hello')]);
  await sessionEvents(child, [output('Child reports')]);
  // the subagent output is indented and labelled in the task chat
  const subagent = page.locator('[data-testid="chat.item"][data-kind="session.output"]', { hasText: 'Child reports' });
  await expect(subagent.getByTestId('chat.item.subagent')).toHaveText('Subagent 1');
  await page.getByTestId('task.tabs.sessions').click();
  await expect(page).toHaveURL(/\?tab=sessions$/);
  const nodes = page.getByTestId('sessions.node');
  await expect(nodes).toHaveCount(3);
  expect(await nodes.evaluateAll((items) => items.map((item) => item.getAttribute('data-depth')))).toEqual([
    '0',
    '1',
    '2',
  ]);
  const childNode = page.locator(`[data-testid="sessions.node"][data-session="${child}"]`);
  await expect(childNode.getByTestId('session.model').first()).toHaveText('Model claude-sonnet');
  // the device has no `device.profile` label here: a sentence, never an ID (KOR-05, B-23, ADR-073)
  await expect(childNode.getByTestId('session.device').first()).toHaveText('On Device without a name (reachable)');
  await expect(childNode.getByTestId('session.state').first()).toHaveAttribute('data-state', 'working');
  await childNode.getByTestId('sessions.link').first().click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/s/${child}$`));
  await expect(page.getByTestId('session.title')).toContainText('Subagent 1');
  await expect(page.getByTestId('terminal.label')).toHaveText('Read-only view – prompts are entered in the chat');
  await page.getByTestId('session.tabs.chat').click();
  await expect(page.getByTestId('chat.output')).toHaveCount(1);
  await expect(page.getByTestId('chat.output')).toContainText('Child reports');
  await page.getByTestId('session.tabs.children').click();
  await expect(page.getByTestId('session.child')).toHaveCount(1);
  await page.getByTestId('session.child').locator('a').click();
  await expect(page).toHaveURL(new RegExp(`/s/${grandchild}$`));
  await expect(page.getByTestId('session.title')).toContainText('Subagent 1.1');
  await page.getByTestId('session.tabs.children').click();
  await expect(page.getByTestId('session.children.empty')).toBeVisible();
  await page.getByTestId('session.back').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}/t/${task}$`));
});

test('an unknown session is an error', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  await page.goto(`/o/${user.org}/s/0190f000-0000-7000-8000-000000000000`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('session.error')).toBeVisible();
});
