// Operations (06-web.md sections 5.2–5.5) against the real hub with the device double registering and finishing
// operations through the device API: cards in the chat with live state, PR link, commit SHA and comment URL, a
// duplicate comment linking its original, a failed push with its error, a rename by the session, a subagent that the
// hub starts (session tree, Children tab with the purpose, the child's terminal and chat), and the Operations tab
// newest first with the detail drawer (transitions, decrypted parameters and result).
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { operation } from '../helpers/operations.ts';
import { sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

const SHA = 'c0ffee'.padEnd(40, '0');

test('shows operation cards, duplicates, renames and the Operations tab', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Open a pull request for the fix');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');

  const pr = await operation(
    main.session_id,
    'github.create_pull_request',
    { repo: 'dfx/agent', head: 'fix-1', base: 'main', title: 'Fix it' },
    { state: 'succeeded', result: { number: 7, url: 'https://github.com/dfx/agent/pull/7', id: 1, draft: true } },
  );
  expect(pr.state).toBe('succeeded');
  const prCard = page.locator(`[data-testid="operation.card"][data-operation="${pr.operation_id}"]`).first();
  await expect(prCard.getByTestId('operation.state')).toHaveAttribute('data-state', 'succeeded');
  await expect(prCard.getByTestId('operation.type')).toHaveText('Create pull request');
  await expect(prCard.getByTestId('operation.subject')).toHaveText('dfx/agent fix-1');
  await expect(prCard.getByTestId('operation.fact').locator('a')).toHaveAttribute(
    'href',
    'https://github.com/dfx/agent/pull/7',
  );
  await expect(page.getByTestId('chat.operation-result').first()).toContainText('Create pull request finished: Done');

  const commit = await operation(
    main.session_id,
    'github.commit',
    { repo: 'dfx/agent', branch: 'fix-1', message: 'Fix it' },
    { state: 'succeeded', result: { commit_sha: SHA, parent_sha: SHA, tree_sha: SHA, files_changed: 1 } },
    { parent_sha: SHA, tree_sha: SHA },
  );
  await expect(
    page.locator(
      `[data-testid="operation.card"][data-operation="${commit.operation_id}"] [data-testid="operation.fact"]`,
    ),
  ).toHaveText(`Commit: ${SHA.slice(0, 12)}`);

  const comment = { repo: 'dfx/agent', number: 7, body: 'Looks good' };
  const first = await operation(main.session_id, 'github.comment', comment, {
    state: 'succeeded',
    result: { comment_id: 9, url: 'https://github.com/dfx/agent/pull/7#issuecomment-9' },
  });
  const again = await operation(main.session_id, 'github.comment', comment);
  expect(again).toMatchObject({ state: 'duplicate', duplicate_of: first.operation_id });
  const duplicate = page.locator(`[data-testid="operation.card"][data-operation="${again.operation_id}"]`);
  await expect(duplicate.getByTestId('operation.state')).toHaveAttribute('data-state', 'duplicate');
  await expect(duplicate.getByTestId('operation.duplicate-of')).toHaveText(
    `Duplicate of ${first.operation_id.slice(0, 8)}`,
  );

  const push = await operation(
    main.session_id,
    'github.push',
    { repo: 'dfx/agent', branch: 'fix-1', commit_sha: SHA },
    { state: 'failed', error: { code: 'non_fast_forward', retryable: false } },
  );
  const pushCard = page.locator(`[data-testid="operation.card"][data-operation="${push.operation_id}"]`);
  await expect(pushCard.getByTestId('operation.error')).toHaveAttribute('data-code', 'non_fast_forward');

  // a live state: accepted, nothing finished yet
  const pending = await operation(main.session_id, 'github.list_repositories', {});
  await expect(
    page.locator(
      `[data-testid="operation.card"][data-operation="${pending.operation_id}"] [data-testid="operation.state"]`,
    ),
  ).toHaveAttribute('data-state', 'accepted');

  await operation(
    main.session_id,
    'task.set_title',
    { title: 'Fix the login bug' },
    { state: 'succeeded', result: {} },
  );
  await expect(page.getByTestId('task.title')).toHaveText('Fix the login bug');
  await expect(page.getByTestId('task.renamed')).toHaveText('Renamed by session');

  // the Operations tab: newest first, the detail of the push
  await page.getByTestId('task.tabs.operations').click();
  await expect(page).toHaveURL(/\?tab=operations$/);
  const items = page.getByTestId('operations.item');
  await expect(items).toHaveCount(7);
  await expect(items.first().getByTestId('operation.type')).toHaveText('Rename task');
  await expect(items.last().getByTestId('operation.type')).toHaveText('Create pull request');
  await page
    .locator(`[data-testid="operations.item"][data-operation="${push.operation_id}"]`)
    .getByTestId('operations.open')
    .click();
  const detail = page.getByTestId('operations.detail');
  await expect(detail.getByTestId('operations.transition')).toHaveCount(4);
  await expect(detail.getByTestId('operations.transition').nth(1)).toContainText('hub');
  await expect(detail.getByTestId('operations.detail.params')).toContainText('"branch": "fix-1"');
  await expect(detail.getByTestId('operations.detail.result')).toContainText('"code": "non_fast_forward"');
  await detail.getByTestId('operations.detail.close').click();
  await expect(detail).toHaveCount(0);
  // the duplicate's link opens its original
  await page
    .locator(`[data-testid="operations.item"][data-operation="${again.operation_id}"]`)
    .getByTestId('operation.duplicate-of')
    .click();
  await expect(page).toHaveURL(new RegExp(`op=${first.operation_id}$`));
  await expect(page.getByTestId('operations.detail').getByTestId('operation.fact').locator('a')).toHaveAttribute(
    'href',
    'https://github.com/dfx/agent/pull/7#issuecomment-9',
  );
  expect(user.device).toBeTruthy();
});

test('shows a subagent the hub starts as a child with its purpose, terminal and chat', async ({ page, baseURL }) => {
  await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Review the change with a helper');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  const sub = await operation(
    main.session_id,
    'subagent.request',
    { purpose: 'Review the pull request', prompt: 'Review PR 7 for security issues.' },
    { state: 'succeeded' },
  );
  expect(sub.child_session_id).toBeTruthy();
  const child = sub.child_session_id!;
  const subCard = page.locator(`[data-testid="operation.card"][data-operation="${sub.operation_id}"]`);
  await expect(subCard.getByTestId('operation.subject')).toHaveText('Review the pull request');
  await expect(subCard.getByTestId('operation.fact').locator('a')).toHaveAttribute('href', new RegExp(`/s/${child}$`));
  // one state per call: each is applied before the call returns (no background walk racing the next registration)
  for (const state of [...START_STEPS, 'working']) await walk(child, [state]);
  await sessionEvents(child, [
    { kind: 'session.output', body: { message_id: 'c1', role: 'assistant', turn: 1, text: 'No issues found.' } },
  ]);
  const grand = await operation(
    child,
    'subagent.request',
    { purpose: 'Check the tests', prompt: 'Run through the tests.' },
    { state: 'succeeded' },
  );
  expect(grand.child_session_id).toBeTruthy();
  // the session tree, depth ≤ 2
  await page.getByTestId('task.tabs.sessions').click();
  await expect(page.getByTestId('sessions.node')).toHaveCount(3);
  // the Children tab of the main session shows the subtree with the purposes
  await page
    .locator(`[data-testid="sessions.node"][data-session="${main.session_id}"]`)
    .getByTestId('sessions.link')
    .first()
    .click();
  await page.getByTestId('session.tabs.children').click();
  const children = page.getByTestId('session.child');
  await expect(children).toHaveCount(2);
  await expect(children.first().getByTestId('children.purpose').first()).toHaveText('Purpose: Review the pull request');
  await expect(children.nth(1)).toHaveAttribute('data-depth', '2');
  await expect(children.nth(1).getByTestId('children.purpose')).toHaveText('Purpose: Check the tests');
  await children.first().getByTestId('session.child.link').first().click();
  await expect(page).toHaveURL(new RegExp(`/s/${child}$`));
  await expect(page.getByTestId('session.title')).toContainText('Subagent 1');
  await expect(page.getByTestId('terminal.label')).toBeVisible();
  await page.getByTestId('session.tabs.chat').click();
  await expect(page.getByTestId('chat.output')).toContainText('No issues found.');
});
