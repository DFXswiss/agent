// Pull requests at a glance and "Fix conflicts" (KOR-48, ADR-084, 06-web.md section 5.6) against the real hub with the
// device double, on the desktop and both phones: a person pastes a pull-request address in the workgroup's "Pull
// requests" view and links it to a task (the hub stores an opaque tag and a sealed body only); the browser asks the
// workgroup's device with `github.pull_status` (the double opens the sealed request and answers the state), shows the
// conflict and sends the fixed platform instruction as the task's next prompt; the situation view offers the same
// button; after the session's merge commit (`github.commit` with `merge: true`) is pushed to the pull request's branch
// the task's panel says "Conflicts fixed, branch updated". A device without a usable GitHub account answers
// `github_not_connected` and the row offers "Connect GitHub" (E-05).
import { expect, type Page } from '@playwright/test';
import { test } from '../fixtures/test.ts';
import { fixConflictsInstruction } from '../../src/core/pulls/instruction';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { answerRequest, operation } from '../helpers/operations.ts';
import { freshUser, openChat, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';
import { navigateInApp } from '../helpers/onboard.ts';
import { visit } from '../helpers/whiteboard.ts';

const REF = { repo: 'example-org/example-repo', number: 12 };
const HEAD = 'a'.repeat(40);
const MERGE = 'c'.repeat(40);

const pullState = (extra: Record<string, unknown> = {}) => ({
  repo: REF.repo,
  number: REF.number,
  found: true,
  title: 'Add the release notes',
  state: 'open',
  draft: false,
  merged: false,
  mergeable: false,
  mergeable_state: 'dirty',
  head_ref: 'release-notes',
  head_sha: HEAD,
  head_repo: REF.repo,
  base_ref: 'main',
  url: `https://github.com/${REF.repo}/pull/${REF.number}`,
  updated_at: '2026-11-24T12:00:00Z',
  ...extra,
});

/** Opens a navigation entry the way a user does: the bottom bar or "More" on a phone, else the main navigation. */
async function openNav(page: Page, id: string): Promise<void> {
  const bar = page.getByTestId(`shell.bottom.${id}`);
  const more = page.getByTestId('shell.bottom.more');
  if (await bar.isVisible()) {
    await bar.click();
  } else if (await more.isVisible()) {
    await more.click();
    await page.getByTestId(`shell.more.${id}`).click();
  } else {
    await page.getByTestId(`shell.nav.${id}`).click();
  }
}

/** Answers the device's open `github.pull_status` request with `body`; returns the opened request body. */
async function answerStatus(device: string, body: unknown): Promise<unknown> {
  let opened: { kind: string; body: unknown } | null = null;
  await expect
    .poll(
      async () => {
        opened = await answerRequest(device, [{ body, final: true }]).catch(() => null);
        return opened?.kind ?? null;
      },
      { timeout: 20_000 },
    )
    .toBe('github.pull_status');
  return opened!.body;
}

test('a linked pull request in conflict is fixed by the task and reported as fixed', async ({ page, baseURL }) => {
  test.setTimeout(180_000);
  const user = await freshUser(page, baseURL!);
  const task = await sendPrompt(page, 'Write the release notes');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working']);
  await expect.poll(async () => (await mainSession(task)).state, { timeout: 20_000 }).toBe('working');
  await openNav(page, 'pulls');
  await expect(page.getByTestId('pulls.screen')).toBeVisible();
  await expect(page.getByTestId('pulls.empty')).toBeVisible();
  // an address that is none
  await page.getByTestId('pulls.link.input').fill('not an address');
  await page.getByTestId('pulls.link.task').selectOption(task);
  await page.getByTestId('pulls.link.submit').click();
  await expect(page.getByTestId('pulls.link.invalid')).toBeVisible();
  await page.getByTestId('pulls.link.input').fill(`https://github.com/${REF.repo}/pull/${REF.number}`);
  await page.getByTestId('pulls.link.submit').click();
  await expect(page.getByTestId('pulls.link.created')).toBeVisible();
  const row = page.getByTestId('pulls.item');
  await expect(row).toHaveAttribute('data-pull', `${REF.repo}#${REF.number}`);
  // the hub holds no repository or number in clear
  const api = await PageUser.open(page, user.recoveryCode);
  const wg = (await api.log(user.org)).state.defaultWg;
  const listed = JSON.stringify(await api.request('GET', `/v1/workgroups/${wg}/pr-links`));
  expect(listed).not.toContain('example-repo');
  expect(listed).toContain(task);
  // the state comes from the device
  const asked = (await answerStatus(user.device, { step: 'done', pulls: [pullState()] })) as Record<string, unknown>;
  expect(asked).toMatchObject({ org_id: user.org, wg_id: wg, pulls: [REF] });
  await expect(row).toHaveAttribute('data-conflict', 'conflict');
  await expect(row.getByTestId('pulls.state')).toHaveText('Open');
  await expect(row.getByTestId('pulls.title')).toHaveText('Add the release notes');
  await expect(row.getByTestId('pulls.github')).toHaveAttribute('href', `https://github.com/${REF.repo}/pull/12`);
  await expect(row.getByTestId('pulls.task')).toHaveText('Task: Write the release notes');
  expect(await layoutProblems(page)).toEqual([]);
  await row.getByTestId('pulls.fix').click();
  await expect(row.getByTestId('pulls.fix.sent')).toBeVisible();
  // the situation view offers the same button on the task's entry
  await openNav(page, 'home');
  const entry = page.getByTestId('attention.item').filter({ hasText: 'Write the release notes' }).first();
  const line = entry.getByTestId('attention.pull');
  await expect(line).toHaveAttribute('data-conflict', 'conflict');
  await expect(entry.getByTestId('attention.fix-conflicts')).toBeVisible();
  // the session merges the base, commits the merge and pushes it to the pull request's branch
  await operation(
    main.session_id,
    'github.commit',
    { repo: REF.repo, branch: 'release-notes', message: 'Merge main', merge: true },
    {
      state: 'succeeded',
      result: {
        commit_sha: MERGE,
        parent_sha: HEAD,
        tree_sha: 'd'.repeat(40),
        files_changed: 2,
        merge_parent: 'e'.repeat(40),
      },
    },
    { parent_sha: HEAD, tree_sha: 'd'.repeat(40) },
  );
  await operation(
    main.session_id,
    'github.push',
    { repo: REF.repo, branch: 'release-notes', commit_sha: MERGE },
    { state: 'succeeded', result: { branch: 'release-notes', sha: MERGE, url: `https://github.com/${REF.repo}` } },
  );
  await entry.getByTestId('attention.open').click();
  await openChat(page); // the chat is the second tab (KONZEPT E-43)
  await expect(page.getByTestId('chat.prompt.text').last()).toHaveText(fixConflictsInstruction(REF));
  await page.getByTestId('task.tabs.pulls').click();
  const panel = page.getByTestId('task.pulls');
  await expect(panel.getByTestId('pulls.fixed')).toContainText('Conflicts fixed, branch updated');
  await expect(panel.getByTestId('pulls.fixed')).toHaveAttribute('data-commit', MERGE);
  // followed by a fresh read: the device answers the merged-in state
  await answerStatus(user.device, { step: 'done', pulls: [pullState({ mergeable: true, head_sha: MERGE })] });
  await expect(panel.getByTestId('pulls.item')).toHaveAttribute('data-conflict', 'clean');
  await expect(panel.getByTestId('pulls.fix')).toHaveCount(0);
});

test('a device without a usable GitHub account asks for the connection in the row', async ({ page, baseURL }) => {
  test.setTimeout(120_000);
  const user = await freshUser(page, baseURL!);
  await sendPrompt(page, 'Check the open pull request');
  await page.getByTestId('task.tabs.pulls').click();
  const panel = page.getByTestId('task.pulls');
  await expect(panel.getByTestId('pulls.empty')).toBeVisible();
  await panel.getByTestId('pulls.link.input').fill(`${REF.repo}#${REF.number}`);
  await panel.getByTestId('pulls.link.submit').click();
  await expect(panel.getByTestId('pulls.link.created')).toBeVisible();
  await answerStatus(user.device, { step: 'failed', code: 'github_not_connected' });
  const row = panel.getByTestId('pulls.item');
  await expect(row).toHaveAttribute('data-state', 'failed');
  await expect(row.getByTestId('pulls.connect')).toBeVisible();
  await expect(row.getByTestId('github.token')).toBeVisible();
  // linking the same pull request again shows the task it is linked to
  await panel.getByTestId('pulls.link.input').fill(`https://github.com/${REF.repo}/pull/${REF.number}`);
  await panel.getByTestId('pulls.link.submit').click();
  await expect(panel.getByTestId('pulls.link.exists.task')).toHaveText('Check the open pull request');
  // "Check again" reads once more; a person's link can be removed
  await row.getByTestId('pulls.recheck').click();
  await answerStatus(user.device, { step: 'done', pulls: [pullState({ head_repo: 'someone/example-repo' })] });
  await expect(row.getByTestId('pulls.fork')).toBeVisible();
  await expect(row.getByTestId('pulls.fix')).toHaveCount(0);
  await row.getByTestId('pulls.unlink').click();
  await expect(panel.getByTestId('pulls.empty')).toBeVisible();
});

test('the view lists the readable workgroups, offers the form only where the user writes, and hides the rest', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(120_000);
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  const research = await api.createWorkgroup(user.org, 'Research', 'write');
  const watch = await api.createWorkgroup(user.org, 'Watch', 'read');
  await visit(page, `/o/${user.org}/pulls`);
  await expect(page.getByTestId('pulls.screen')).toBeVisible();
  const levels = page.getByTestId('pulls.level');
  await expect(levels).toHaveCount(3);
  await expect(levels.first()).toHaveAttribute('aria-current', 'page');
  await page.locator(`[data-testid="pulls.level"][data-wg="${research}"]`).click();
  await expect(page.getByTestId('pulls.screen')).toHaveAttribute('data-wg', research);
  await expect(page.getByTestId('pulls.link.input')).toBeVisible();
  await page.locator(`[data-testid="pulls.level"][data-wg="${watch}"]`).click();
  await expect(page.getByTestId('pulls.screen')).toHaveAttribute('data-wg', watch);
  await expect(page.getByTestId('pulls.empty')).toBeVisible();
  await expect(page.getByTestId('pulls.link.input')).toHaveCount(0);
  // in-app navigation: a page load would drop the coverage of the page before
  await navigateInApp(page, `/o/${user.org}/w/${'0'.repeat(8)}-0000-7000-8000-${'0'.repeat(12)}/pulls`);
  await expect(page.getByTestId('pulls.not-found')).toBeVisible();
  // an organisation the user has no log of stays "loading"
  await navigateInApp(page, `/o/${'1'.repeat(8)}-0000-7000-8000-${'0'.repeat(12)}/pulls`);
  await expect(page.getByTestId('pulls.screen.loading')).toBeVisible();
});
