// The situation view (KOR-39, ADR-076, 06-web.md section 4.0) against the real hub and the device double, on the
// desktop and on the phones (`MOBILE_DEVICE_SPECS`): an agent's question waits under "Needs your decision" and is
// answered from the view; an approval with a comment; a refused action, a missing GitHub connection and a failed
// session under their groups with their next step; the agent tree of a running task; a member never sees a task of a
// workgroup they cannot read, not even in a counter. The hub derives every category; the browser only shows it. The
// `task.attention` notification is not produced here (the device double applies transitions without the hub's
// notification signals); its text and target are covered in `tests/ui/attention.test.tsx`.
import { expect, type Page } from '@playwright/test';
import { test } from '../fixtures/test.ts';
import { addAuthenticator } from '../helpers/authenticator.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { joinWithCode } from '../helpers/members.ts';
import { onboard } from '../helpers/onboard.ts';
import { operation } from '../helpers/operations.ts';
import { childSession } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

/** Opens Home the way a user does: the bottom bar on a phone, else the main navigation. */
async function home(page: Page): Promise<void> {
  const bottom = page.getByTestId('shell.bottom.home');
  if (await bottom.isVisible()) await bottom.click();
  else await page.getByTestId('shell.nav.home').click();
  await expect(page.getByTestId('attention.screen')).toBeVisible();
}

/** Opens `path` with a reload: the identity is locked again and unlocks with the passkey. */
async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

/** The task's main session, started and working (as the runner reports it; the director walks in the background). */
async function working(task: string): Promise<string> {
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working']);
  await expect.poll(async () => (await mainSession(task)).state, { timeout: 20_000 }).toBe('working');
  return main.session_id;
}

/** The main session asks a person with `type` and ends its turn. */
async function ask(session: string, type: 'human.question' | 'human.approval', question: string): Promise<void> {
  await operation(session, type, { question }, { state: 'succeeded', result: {} });
  await walk(session, ['idle']);
}

const entryIn = (page: Page, category: string, title: string) =>
  page.getByTestId(`attention.group.${category}`).getByTestId('attention.item').filter({ hasText: title });

test('a question waits under "Needs your decision" and is answered from the view', async ({ page, baseURL }) => {
  test.setTimeout(120_000);
  await freshUser(page, baseURL!);
  await expect(page.getByTestId('composer.chips')).toBeVisible();
  const task = await sendPrompt(page, 'Prepare the release notes');
  await ask(await working(task), 'human.question', 'Which version should the notes cover?');
  await home(page);
  const entry = entryIn(page, 'needs_decision', 'Prepare the release notes');
  await expect(entry.getByTestId('attention.item.reason')).toHaveText('The agent has a question');
  await expect(entry.getByTestId('attention.question')).toHaveText('Which version should the notes cover?');
  await expect(entry.getByTestId('tasks.row.owner')).toHaveText('Ada Lovelace');
  await expect(page.getByTestId('attention.counter.mine')).toHaveAttribute('data-count', '1');
  await expect(page.locator('[data-testid="shell.badge.attention"]:visible')).toHaveAttribute('data-count', '1');
  expect(await layoutProblems(page)).toEqual([]);
  await entry.getByTestId('attention.answer').fill('Version 2.4');
  await entry.getByTestId('attention.answer.send').click();
  // the answer is the task's next prompt: the hub moves the task out of the group and the browser follows its frame
  await expect(page.getByTestId('attention.group.needs_decision').getByTestId('attention.item')).toHaveCount(0);
  await expect(page.getByTestId('attention.counter.mine')).toHaveAttribute('data-count', '0');
  await expect(page.getByTestId('shell.badge.attention')).toHaveCount(0);
  await entryIn(page, 'done', 'Prepare the release notes')
    .or(entryIn(page, 'working', 'Prepare the release notes'))
    .getByTestId('attention.open')
    .click();
  await expect(page.getByTestId('chat.prompt.text').last()).toHaveText('Version 2.4');
});

test('an approval is given with a comment and starts with exactly "Approved."', async ({ page, baseURL }) => {
  test.setTimeout(120_000);
  const user = await freshUser(page, baseURL!);
  const task = await sendPrompt(page, 'Deploy the website');
  await ask(await working(task), 'human.approval', 'May I deploy version 3 to production?');
  await home(page);
  const entry = entryIn(page, 'needs_decision', 'Deploy the website');
  await expect(entry.getByTestId('attention.item.reason')).toHaveText('The agent asks for your approval');
  await expect(entry.getByTestId('attention.question')).toHaveText('May I deploy version 3 to production?');
  await expect(entry.getByTestId('attention.decline')).toBeVisible();
  await entry.getByTestId('attention.comment').fill('Only after 18:00');
  await entry.getByTestId('attention.approve').click();
  await expect(page.getByTestId('attention.group.needs_decision').getByTestId('attention.item')).toHaveCount(0);
  await visit(page, `/o/${user.org}/t/${task}`);
  await expect(page.getByTestId('chat.prompt.text').last()).toHaveText(/^Approved\.\s+Only after 18:00$/);
});

test('a missing connection, a refused action and a failed session show under their groups with the next step', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(150_000);
  const user = await freshUser(page, baseURL!);
  const first = await sendPrompt(page, 'Open the pull request');
  const api = await PageUser.open(page, user.recoveryCode);
  const wg = (await api.log(user.org)).state.defaultWg;
  const [refused, failing] = await api.createTasks(user.org, wg, ['Push the hotfix', 'Build the site']);
  const pr = await working(first);
  await operation(
    pr,
    'github.create_pull_request',
    { repo: 'dfx/agent', head: 'fix-1', base: 'main', title: 'Fix it' },
    { state: 'failed', error: { code: 'github_not_connected', retryable: false } },
  );
  await walk(pr, ['idle']);
  const push = await working(refused!);
  await operation(
    push,
    'github.push',
    { repo: 'dfx/agent', branch: 'main', commit_sha: 'a'.repeat(40) },
    { state: 'failed', error: { code: 'branch_protected', retryable: false } },
  );
  await walk(push, ['idle']);
  const build = await working(failing!);
  await walk(build, ['failed'], { reason: 'agent_exited' });
  await home(page);
  const connect = entryIn(page, 'needs_decision', 'Open the pull request');
  await expect(connect.getByTestId('attention.item.reason')).toHaveText('GitHub is not connected');
  const blocked = entryIn(page, 'blocked', 'Push the hotfix');
  await expect(blocked.getByTestId('attention.item.reason')).toHaveText('An action was refused');
  // a refused action has no other next step than opening the task
  await expect(blocked.getByTestId('attention.actions')).toHaveCount(0);
  await expect(blocked.getByTestId('attention.open')).toBeVisible();
  const crashed = entryIn(page, 'blocked', 'Build the site');
  await expect(crashed.getByTestId('attention.item.reason')).toHaveText('The agent stopped with an error');
  await expect(page.getByTestId('attention.counter.blocked')).toHaveAttribute('data-count', '2');
  // "Try again" sends the newest prompt again: a new main session starts and the task leaves "Stuck"
  await crashed.getByTestId('attention.retry').click();
  await expect(entryIn(page, 'blocked', 'Build the site')).toHaveCount(0);
  await expect(entryIn(page, 'working', 'Build the site')).toBeVisible();
  await connect.getByTestId('attention.connect').click();
  await expect(page).toHaveURL(/\/settings$/);
});

test('a running task shows its phase and opens its agent tree with the subagents below their parent', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(120_000);
  await freshUser(page, baseURL!);
  const task = await sendPrompt(page, 'Review the repository');
  const main = await working(task);
  const reviewer = await childSession(main);
  await childSession(main, { state: 'idle' });
  await childSession(reviewer);
  await home(page);
  const entry = entryIn(page, 'working', 'Review the repository');
  await expect(entry.getByTestId('attention.item.reason')).toHaveText('Working');
  await expect(entry.getByTestId('attention.item.since')).toContainText('Running for');
  await expect(entry.getByTestId('attention.tree')).toHaveCount(0);
  await entry.getByTestId('attention.item.toggle').click();
  const nodes = entry.getByTestId('attention.tree.node');
  await expect(nodes).toHaveCount(4);
  expect(await nodes.evaluateAll((items) => items.map((item) => (item as HTMLElement).dataset.depth))).toEqual([
    '0',
    '1',
    '2',
    '1',
  ]);
  await expect(nodes.getByTestId('attention.tree.state')).toHaveText(['Working', 'Working', 'Working', 'Idle']);
  await expect(nodes.nth(2)).toContainText('Subagent 1.1');
  await expect(entry.getByTestId('attention.item.toggle')).toHaveAttribute('aria-expanded', 'true');
});

test('a member never sees a task of a workgroup they cannot read, not even in a counter', async ({
  page,
  baseURL,
  newContext,
}) => {
  test.setTimeout(180_000);
  const origin = baseURL!;
  const adaPage = await (
    await newContext({ viewport: { width: 1280, height: 720 }, isMobile: false, hasTouch: false })
  ).newPage();
  const ada = await freshUser(adaPage, origin);
  await sendPrompt(adaPage, 'Plan the product launch');
  const adaUser = await PageUser.open(adaPage, ada.recoveryCode);
  await adaPage.getByTestId('shell.nav.members').click();
  await adaPage.getByTestId('members.invite.open').click();
  await adaPage.getByTestId('members.invite.name').fill('Bob Member');
  await adaPage.getByTestId('members.invite.submit').click();
  const code = (await adaPage.getByTestId('members.invite.code').textContent())!;
  await adaPage.getByTestId('members.invite.done').click();
  await addAuthenticator(page);
  const bob = await onboard(page, { origin, name: 'Bob Member' });
  const bobUser = await PageUser.open(page, bob.recoveryCode);
  await joinWithCode(bobUser, origin, code, 'Bob Member');
  const research = await adaUser.createWorkgroup(ada.org, 'Research', 'write', {
    readers: [bobUser.userId],
    assignDevices: true,
  });
  const secret = await adaUser.createWorkgroup(ada.org, 'Secret', 'write', { assignDevices: true });
  const [hidden] = await adaUser.createTasks(ada.org, secret, ['Salary list of the secret team']);
  const [shown] = await adaUser.createTasks(ada.org, research, ['Market report for Switzerland']);
  // both ask a question: only the readable one reaches Bob's view (without controls: Bob only reads Research)
  await ask(await working(hidden!), 'human.question', 'Which salaries?');
  await ask(await working(shown!), 'human.question', 'Which markets?');

  const requested: string[] = [];
  page.on('request', (request) => requested.push(request.url()));
  await visit(page, `/o/${ada.org}`);
  const decisions = page.getByTestId('attention.group.needs_decision').getByTestId('attention.item');
  await expect(decisions).toHaveCount(1);
  await expect(decisions.getByTestId('tasks.row.title')).toHaveText('Market report for Switzerland');
  await expect(decisions.getByTestId('tasks.row.wg')).toHaveText('Research');
  await expect(decisions.getByTestId('tasks.row.owner')).toHaveText('Ada Lovelace');
  await expect(decisions.getByTestId('attention.actions')).toHaveCount(0);
  await expect(decisions.getByTestId('attention.open')).toBeVisible();
  await expect(page.getByTestId('attention.group.needs_decision').getByTestId('attention.group.count')).toHaveText(
    '(1)',
  );
  // Bob owns none of them: nothing waits for him
  await expect(page.getByTestId('attention.counter.mine')).toHaveAttribute('data-count', '0');
  await expect(page.getByText('Salary list of the secret team')).toHaveCount(0);
  expect(requested.filter((url) => url.includes(secret) || url.includes(hidden!))).toEqual([]);
  // Ada sees both, owns both, and may answer
  await home(adaPage);
  await expect(adaPage.getByTestId('attention.group.needs_decision').getByTestId('attention.item')).toHaveCount(2);
  await expect(adaPage.getByTestId('attention.counter.mine')).toHaveAttribute('data-count', '2');
  await expect(adaPage.getByTestId('attention.answer')).toHaveCount(2);
});
