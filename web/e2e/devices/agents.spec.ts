// Main agents with names, their people and their actors (KOR-52, ADR-095, 06-web.md section 4.0a) against the real hub
// and the device double, on the desktop and on the phones (`MOBILE_DEVICE_SPECS`):
// - by person: the switch kept in the URL, the user's own row first ("You") with the counts, the agents with their
//   automatic names and handles (`#1`, `#2`), the GitHub identity they act as ("Automatic"; a chosen account is
//   covered by E2E-71 with the runner), their helpers and the agent tree with the subagent's handle and purpose;
// - rename: the task page names the agent `<name> #<number>`, "Rename" seals `task.agent_name` (the hub holds the
//   header only), the name survives a reload and follows into the person view, "Automatic name" goes back;
// - actor: the audit names the main agent and the subagent as actors ("Main agent <name> #1 …", "Subagent #1.1 of
//   <name> #1 …") and filters by the person they acted for.
import { expect, type Page } from '@playwright/test';
import { automaticName } from '../../src/core/agents/name';
import { test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems, MIN_TARGET } from '../helpers/layout-checks.ts';
import { operation } from '../helpers/operations.ts';
import { freshUser, openChat, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';
import { navigateInApp, visit } from '../helpers/whiteboard.ts';

const LOGIN_TITLE = 'Fix the login bug in the old customer portal';
const NOTES_TITLE = 'Draft the release notes';

/** Opens Home the way a user does: the bottom bar on a phone, else the main navigation. */
async function home(page: Page): Promise<void> {
  const bottom = page.getByTestId('shell.bottom.home');
  if (await bottom.isVisible()) await bottom.click();
  else await page.getByTestId('shell.nav.home').click();
  await expect(page.getByTestId('attention.screen')).toBeVisible();
}

/** The task's main session, started and working (as the runner reports it). */
async function working(task: string): Promise<string> {
  const main = await mainSession(task);
  for (const state of [...START_STEPS, 'working']) await walk(main.session_id, [state]);
  await expect.poll(async () => (await mainSession(task)).state, { timeout: 20_000 }).toBe('working');
  return main.session_id;
}

/** A subagent of `session` requested with `purpose`, started and working; returns its session. */
async function subagent(session: string, purpose: string): Promise<string> {
  const sub = await operation(session, 'subagent.request', { purpose, prompt: `${purpose}.` }, { state: 'succeeded' });
  const child = sub.child_session_id!;
  for (const state of [...START_STEPS, 'working']) await walk(child, [state]);
  return child;
}

const agentRow = (page: Page, task: string) => page.locator(`[data-testid="agents.item"][data-task="${task}"]`);

/** The height of each visible element of `testIds` is at least `MIN_TARGET`. */
async function targetsFit(page: Page, testIds: readonly string[]): Promise<void> {
  for (const id of testIds) {
    const box = await page.getByTestId(id).first().boundingBox();
    expect(box, id).not.toBeNull();
    expect(box!.height, id).toBeGreaterThanOrEqual(MIN_TARGET);
  }
}

test('groups the situation by person with the agents, their handles, identities and helpers', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(180_000);
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await home(page);
  const login = await sendPrompt(page, LOGIN_TITLE);
  const main = await working(login);
  await subagent(main, 'Review the login form');
  await home(page);
  const notes = await sendPrompt(page, NOTES_TITLE);
  await home(page);

  await expect(page.getByTestId('agents.switch')).toHaveAttribute('data-by', 'situation');
  await page.getByTestId('agents.switch.person').click();
  await expect(page).toHaveURL(/\?by=person$/);
  await expect(page.getByTestId('agents.switch')).toHaveAttribute('data-by', 'person');
  // the counters stay, the groups give way to the people
  await expect(page.getByTestId('attention.counters')).toBeVisible();
  await expect(page.getByTestId('attention.group.working')).toHaveCount(0);
  const me = page.locator(`[data-testid="agents.person"][data-owner="${api.userId}"]`);
  await expect(page.getByTestId('agents.person').first()).toHaveAttribute('data-owner', api.userId);
  await expect(me.getByTestId('agents.person.name')).toHaveText('You');
  await expect(me.getByTestId('agents.person.count.agents')).toHaveText('2');
  await me.getByTestId('agents.person.toggle').click();
  await expect(me.getByTestId('agents.item')).toHaveCount(2);
  const first = agentRow(page, login);
  await expect(first.getByTestId('agents.item.name')).toHaveText(automaticName(LOGIN_TITLE));
  await expect(first.getByTestId('agents.item.handle')).toHaveText('#1');
  await expect(first.getByTestId('agents.item.identity')).toHaveText('GitHub: Automatic');
  await expect(first.getByTestId('agents.item.subagents')).toHaveAttribute('data-total', '1');
  const second = agentRow(page, notes);
  await expect(second.getByTestId('agents.item.name')).toHaveText(NOTES_TITLE);
  await expect(second.getByTestId('agents.item.handle')).toHaveText('#2');
  await expect(second.getByTestId('agents.item.identity')).toHaveText('GitHub: Automatic');
  // the agent tree names the subagent by its handle with the purpose its main agent gave
  await first.getByTestId('agents.item.toggle').click();
  const nodes = first.getByTestId('attention.tree.node');
  await expect(nodes).toHaveCount(2);
  expect(await nodes.evaluateAll((items) => items.map((item) => (item as HTMLElement).dataset.handle))).toEqual([
    '#1',
    '#1.1',
  ]);
  await expect(nodes.nth(1)).toContainText('Subagent #1.1');
  await expect(nodes.nth(1).getByTestId('children.purpose')).toHaveText('Purpose: Review the login form');
  await targetsFit(page, ['agents.switch.person', 'agents.person.toggle', 'agents.item.toggle', 'agents.item.rename']);
  expect(await layoutProblems(page)).toEqual([]);
  // the view by person survives a reload (the URL keeps it)
  await visit(page, `/o/${user.org}?by=person`);
  await expect(page.getByTestId('agents.switch')).toHaveAttribute('data-by', 'person');
  await expect(page.getByTestId('agents.person')).toHaveCount(1);
});

test('renames an agent, keeps the name across a reload and names the agents as actors in the audit', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(180_000);
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  const task = await sendPrompt(page, 'Prepare the quarterly report');
  await expect(page.getByTestId('task.agent.name')).toHaveText('Prepare the quarterly report');
  await expect(page.getByTestId('task.agent.handle')).toHaveText('#1');
  await page.getByTestId('task.agent.rename').click();
  await expect(page.getByTestId('task.agent.rename.input')).toHaveValue('Prepare the quarterly report');
  await page.getByTestId('task.agent.rename.input').fill('x'.repeat(81));
  await expect(page.getByTestId('task.agent.rename.save')).toBeDisabled();
  await page.getByTestId('task.agent.rename.input').fill('Report writer');
  await page.getByTestId('task.agent.rename.save').click();
  await expect(page.getByTestId('task.agent.name')).toHaveText('Report writer');
  await expect(page.getByTestId('task.agent.rename.input')).toHaveCount(0);
  // the hub stores the sealed event: the name comes back after a reload (the task row names the event)
  await visit(page, `/o/${user.org}/t/${task}`);
  await openChat(page); // the task's header is on every tab but "Claude Code" (KONZEPT E-43)
  await expect(page.getByTestId('task.agent.name')).toHaveText('Report writer');
  // back to the automatic name and to a given one again
  await page.getByTestId('task.agent.rename').click();
  await page.getByTestId('task.agent.rename.automatic').click();
  await expect(page.getByTestId('task.agent.name')).toHaveText('Prepare the quarterly report');
  await page.getByTestId('task.agent.rename').click();
  await page.getByTestId('task.agent.rename.input').fill('Report writer');
  await page.getByTestId('task.agent.rename.save').click();
  await expect(page.getByTestId('task.agent.name')).toHaveText('Report writer');
  // the task list row shows the name with the handle
  await navigateInApp(page, `/o/${user.org}?by=person`);
  await page.getByTestId('agents.person.toggle').click();
  await expect(agentRow(page, task).getByTestId('agents.item.name')).toHaveText('Report writer');

  // the main agent and its subagent act: the audit names them as actors
  const main = await working(task);
  const child = await subagent(main, 'Collect the figures');
  await operation(child, 'human.question', { question: 'Which quarter?' }, { state: 'succeeded', result: {} });
  await visit(page, `/o/${user.org}/audit?task=${task}&action=operation`);
  const mainActor = page.locator('[data-testid="audit.row.actor"][data-actor-kind="main_agent"]').first();
  await expect(mainActor).toHaveText(/^Main agent Report writer #1 (on behalf of .+|\(no single person\))$/, {
    timeout: 30_000,
  });
  const subActor = page.locator('[data-testid="audit.row.actor"][data-actor-kind="subagent"]').first();
  await expect(subActor).toHaveText(/^Subagent #1\.1 of Report writer #1 (on behalf of .+|\(no single person\))$/);
  // the filter "On behalf of" goes into the URL and the hub's query
  const requested: string[] = [];
  page.on('request', (request) => requested.push(request.url()));
  await page.getByTestId('audit.filter.on_behalf_of').selectOption(api.userId);
  await page.getByTestId('audit.filter.apply').click();
  await expect(page).toHaveURL(new RegExp(`on_behalf_of=${api.userId}`));
  await expect
    .poll(() => requested.some((url) => url.includes('/audit?') && url.includes(`on_behalf_of=${api.userId}`)))
    .toBe(true);
  await expect(page.getByTestId('audit.rows').or(page.getByTestId('audit.empty'))).toBeVisible();
  expect(await layoutProblems(page)).toEqual([]);
});
