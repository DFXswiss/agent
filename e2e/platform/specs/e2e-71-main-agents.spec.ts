// E2E-71 (KOR-52, ADR-095; 03-api.md sections 4.6, 4.11, 8.1; 06-web.md section 4.0a): two people with several main
// agents and subagents.
// - Names: every task's main agent has the handle #<number> of its organization and an automatic name from its title;
//   a subagent is #<number>.<path>. Each subagent asks `get_context` and learns its kind, handle, main agent and
//   parent.
// - People: the situation view by person lists the user first and every other person with the open main agents in
//   the workgroups the user reads; a task of a workgroup Wendy cannot read never shows up for her (not even in a
//   count).
// - Identity: Ada's second agent acts as her own GitHub account and shows it; the others are "Automatic".
// - Rename: Wendy (write access) renames Ada's first agent; both see the new name, the hub holds only the header of
//   the sealed `task.agent_name` event.
// - Actor: the audit names the main agent or the subagent as actor, on behalf of the person who instructed it, and the
//   audit view prints it so.
// The hub holds none of the texts (plaintext scan). Runs on T1.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { connectGitHubToken, seedGitHub } from '../fixtures/github.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { inviteMember, joinAsNewUser } from '../fixtures/team.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { automaticName } from '../../../web/src/core/agents/name.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-71-main-agents.json', import.meta.url), 'utf8'),
);
const ADA_NAME = 'Ada kz65-name-a7';
const WENDY_NAME = 'Wendy kz65-name-w2';
const ADA_LOGIN = 'kz65-ada-x3';
/** Not GitHub-shaped on purpose: a token of the double only. */
const ADA_TOKEN = ['kz65', 'ada', 'tok', 'm4p8'].join('-');
const A1 = 'E2E71-A1 kz65-prompt-a1: review the login flow';
const A2 = 'E2E71-A2 kz65-prompt-a2: draft the release notes';
const A3 = 'E2E71-A3 kz65-prompt-a3: plan the hidden budget';
const W1 = 'E2E71-W1 kz65-prompt-w1: check the documentation';
const W2 = 'E2E71-W2 kz65-prompt-w2: sort the changelog';
const HIDDEN = 'Hidden kz65-wg-h5';
const RENAMED = 'Login doctor kz65-name-r1';
const CANARIES = [
  'kz65-name-a7',
  'kz65-name-w2',
  'kz65-prompt-a1',
  'kz65-prompt-a2',
  'kz65-prompt-a3',
  'kz65-prompt-w1',
  'kz65-prompt-w2',
  'kz65-purpose-a1',
  'kz65-purpose-w1',
  'kz65-child-a',
  'kz65-child-w',
  'kz65-done-a2',
  'kz65-done-w2',
  'kz65-done-a3',
  'kz65-wg-h5',
  'kz65-name-r1',
];

interface JournalRecord {
  readonly path: string;
  readonly step?: string | null;
  readonly body: unknown;
}

/** The conversation of the newest request the model double answered with step `step`. */
async function requestOf(orchestrator: Orchestrator, step: string): Promise<string> {
  const { body } = await orchestrator.doubleRequest('model', 'GET', '/_double/journal');
  const records = (Array.isArray(body) ? body : []) as JournalRecord[];
  const found = records.filter((record) => record.path === '/v1/messages' && record.step === step).at(-1);
  expect(found, `no request answered by step ${step}`).toBeDefined();
  return JSON.stringify((found!.body as { messages?: unknown }).messages);
}

/** Waits until the task's newest main session has ended its turn. */
async function settled(orchestrator: Orchestrator, task: string): Promise<string> {
  await expect
    .poll(async () => (await mainSessions(orchestrator, task)).at(-1)?.state, { timeout: 180_000 })
    .toMatch(/^(idle|stopped)$/);
  return (await mainSessions(orchestrator, task)).at(-1)!.id;
}

/** The task's child sessions (hub rows). */
const children = (orchestrator: Orchestrator, task: string) =>
  orchestrator.hubQuery<{ id: string; agent_path: string; parent_session_id: string; state: string }>(
    `SELECT id::text, agent_path, parent_session_id::text, state FROM hub.session WHERE task_id = '${uuid(task)}'` +
      ' AND depth > 0 ORDER BY created_at',
  );

/** Waits until every child of the task has ended. */
async function childrenDone(orchestrator: Orchestrator, task: string): Promise<void> {
  await expect
    .poll(async () => (await children(orchestrator, task)).map((child) => child.state), { timeout: 240_000 })
    .toEqual([expect.stringMatching(/^(idle|stopped)$/)]);
}

interface ActorRow {
  readonly action: string;
  readonly actor_kind: string;
  readonly actor_id: string | null;
  readonly session_id: string | null;
  readonly on_behalf_of: string | null;
}

/** The task's audit rows written for its sessions (operations, child starts, platform reads). */
const actorRows = (orchestrator: Orchestrator, task: string) =>
  orchestrator.hubQuery<ActorRow>(
    'SELECT action, actor_kind, actor_id::text, session_id::text, on_behalf_of::text FROM hub.audit' +
      ` WHERE task_id = '${uuid(task)}' AND (action LIKE 'operation.%' OR action = 'session.requested'` +
      " OR action LIKE 'read.session.%') ORDER BY seq",
  );

/** Checks the actors of a task whose main agent requested one subagent that read `get_context`. */
async function expectActors(orchestrator: Orchestrator, task: string, person: string): Promise<void> {
  const [main] = await mainSessions(orchestrator, task);
  const [child] = await children(orchestrator, task);
  const rows = await actorRows(orchestrator, task);
  const operations = rows.filter((row) => row.action.startsWith('operation.'));
  expect(operations.length).toBeGreaterThan(0);
  for (const row of operations)
    expect([row.actor_kind, row.actor_id, row.on_behalf_of]).toEqual(['main_agent', main!.id, person]);
  expect(rows.filter((row) => row.action === 'session.requested' && row.session_id === child!.id)).toEqual([
    {
      action: 'session.requested',
      actor_kind: 'main_agent',
      actor_id: main!.id,
      session_id: child!.id,
      on_behalf_of: person,
    },
  ]);
  const reads = rows.filter((row) => row.action === 'read.session.get_context');
  expect(reads.length).toBeGreaterThan(0);
  for (const row of reads)
    expect([row.actor_kind, row.actor_id, row.on_behalf_of]).toEqual(['subagent', child!.id, person]);
}

/** The person rows of the situation view by person: owner IDs in order. */
async function people(page: Page, org: string): Promise<string[]> {
  await navigateInApp(page, `/o/${org}?by=person`);
  const rows = page.getByTestId('agents.person');
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });
  const owners: string[] = [];
  for (let i = 0; i < (await rows.count()); i += 1) owners.push((await rows.nth(i).getAttribute('data-owner'))!);
  return owners;
}

const person = (page: Page, owner: string) => page.locator(`[data-testid="agents.person"][data-owner="${owner}"]`);
const agentRow = (page: Page, task: string) => page.locator(`[data-testid="agents.item"][data-task="${task}"]`);

/** Opens the agents of `owner` in the view by person (a no-op when they are open). */
async function openPerson(page: Page, owner: string): Promise<void> {
  const toggle = person(page, owner).getByTestId('agents.person.toggle');
  await expect(async () => {
    if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true', { timeout: 2_000 });
  }).toPass({ timeout: 30_000 });
}

test.describe.configure({ mode: 'serial' });

test('@E2E-71 @needs-vm main agents have names and handles, people see their agents with GitHub identities, the audit names the agent on behalf of a person', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, {
    users: [
      {
        login: ADA_LOGIN,
        id: 7651,
        name: null,
        email: null,
        tokens: [{ token: ADA_TOKEN, kind: 'classic', scopes: ['repo', 'read:org'] }],
      },
    ],
    repositories: [],
  });
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, ADA_NAME);
  const org = onboarded.org;
  const ada = await PageUser.open(page, onboarded.recoveryCode);
  await connectGitHubToken(page, ADA_TOKEN, ADA_LOGIN);
  const [identity] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT id::text FROM hub.credential WHERE owner_id = '${uuid(ada.userId)}' AND kind = 'github'`,
  );
  const hidden = await ada.createWorkgroup(org, HIDDEN);
  const wendy = await joinAsNewUser(browser, e2e, org, await inviteMember(page, org, {}), WENDY_NAME);
  const wendyId = wendy.user.userId;

  // Ada: a main agent with a subagent, one acting as her GitHub account, one in a workgroup only she reads.
  await home(page);
  const a1 = await sendPrompt(page, A1);
  await expectAnswerInChat(page, 'A helper checks the login.');
  await childrenDone(orchestrator, a1);
  await settled(orchestrator, a1);
  await home(page);
  await page.getByTestId('composer.identity').selectOption(identity!.id);
  const a2 = await sendPrompt(page, A2);
  await expectAnswerInChat(page, 'Release notes drafted kz65-done-a2.');
  await home(page);
  await expect(page.getByTestId('home.wg').locator(`option[value="${hidden}"]`)).toHaveCount(1, { timeout: 30_000 });
  await page.getByTestId('home.wg').selectOption(hidden);
  const a3 = await sendPrompt(page, A3);

  // Wendy: a main agent with a subagent and a second one.
  await home(wendy.page);
  const w1 = await sendPrompt(wendy.page, W1);
  await expectAnswerInChat(wendy.page, 'A helper checks the docs.');
  await childrenDone(orchestrator, w1);
  await settled(orchestrator, w1);
  await home(wendy.page);
  const w2 = await sendPrompt(wendy.page, W2);
  await expectAnswerInChat(wendy.page, 'Changelog sorted kz65-done-w2.');

  // Handles: numbers per organization in creation order, subagents below their main agent.
  const numbers = await orchestrator.hubQuery<{ id: string; number: string }>(
    `SELECT id::text, number::text FROM hub.task WHERE org_id = '${uuid(org)}' ORDER BY number`,
  );
  expect(numbers).toEqual([
    { id: a1, number: '1' },
    { id: a2, number: '2' },
    { id: a3, number: '3' },
    { id: w1, number: '4' },
    { id: w2, number: '5' },
  ]);
  const [a1Main] = await mainSessions(orchestrator, a1);
  const [w1Main] = await mainSessions(orchestrator, w1);
  expect((await children(orchestrator, a1)).map((c) => [c.agent_path, c.parent_session_id])).toEqual([
    ['1', a1Main!.id],
  ]);
  expect((await children(orchestrator, w1)).map((c) => [c.agent_path, c.parent_session_id])).toEqual([
    ['1', w1Main!.id],
  ]);

  // The subagents know who they are.
  const a1Name = automaticName(A1);
  const childA = await requestOf(orchestrator, 'child-a-known');
  for (const part of [
    '\\"kind\\":\\"subagent\\"',
    '\\"handle\\":\\"#1.1\\"',
    `\\"main\\":{\\"handle\\":\\"#1\\",\\"name\\":\\"${a1Name}\\"}`,
    `\\"parent\\":{\\"handle\\":\\"#1\\",\\"session_id\\":\\"${a1Main!.id}\\"}`,
  ])
    expect(childA).toContain(part);
  expect(await requestOf(orchestrator, 'child-w-known')).toContain(`\\"name\\":\\"${automaticName(W1)}\\"`);

  // By person: Wendy first, then Ada with the two agents Wendy may read; the hidden one never shows up.
  expect(await people(wendy.page, org)).toEqual([wendyId, ada.userId]);
  await expect(person(wendy.page, ada.userId).getByTestId('agents.person.count.agents')).toHaveText('2');
  await expect(person(wendy.page, wendyId).getByTestId('agents.person.count.agents')).toHaveText('2');
  await openPerson(wendy.page, ada.userId);
  await expect(person(wendy.page, ada.userId).getByTestId('agents.item')).toHaveCount(2);
  await expect(agentRow(wendy.page, a3)).toHaveCount(0);
  await expect(agentRow(wendy.page, a1).getByTestId('agents.item.handle')).toHaveText('#1');
  await expect(agentRow(wendy.page, a1).getByTestId('agents.item.name')).toHaveText(a1Name);
  await expect(agentRow(wendy.page, a2).getByTestId('agents.item.handle')).toHaveText('#2');

  // Ada sees herself first with all three agents and her GitHub account at the second.
  expect(await people(page, org)).toEqual([ada.userId, wendyId]);
  await expect(person(page, ada.userId).getByTestId('agents.person.count.agents')).toHaveText('3');
  await openPerson(page, ada.userId);
  await expect(agentRow(page, a3).getByTestId('agents.item.handle')).toHaveText('#3');
  await expect(agentRow(page, a2).getByTestId('agents.item.identity')).toContainText(ADA_LOGIN);
  await expect(agentRow(page, a1).getByTestId('agents.item.identity')).toHaveText(/Automatic/);
  await expect(agentRow(page, a1).getByTestId('agents.item.subagents')).toBeVisible();

  // Wendy renames Ada's first agent; both see the new name with the handle.
  await navigateInApp(wendy.page, `/o/${org}/t/${a1}`);
  await expect(wendy.page.getByTestId('task.agent.handle')).toHaveText('#1');
  await wendy.page.getByTestId('task.agent.rename').click();
  await wendy.page.getByTestId('task.agent.rename.input').fill(RENAMED);
  await wendy.page.getByTestId('task.agent.rename.save').click();
  await expect(wendy.page.getByTestId('task.agent.name')).toHaveText(RENAMED, { timeout: 30_000 });
  const named = await orchestrator.hubQuery<{ author: string }>(
    `SELECT author_id::text AS author FROM hub.event WHERE task_id = '${uuid(a1)}' AND kind = 'task.agent_name'`,
  );
  expect(named).toEqual([{ author: wendyId }]);
  await people(page, org);
  await openPerson(page, ada.userId);
  await expect(agentRow(page, a1).getByTestId('agents.item.name')).toHaveText(RENAMED, { timeout: 30_000 });

  // The audit names the agents on behalf of the person who instructed them.
  await expectActors(orchestrator, a1, ada.userId);
  await expectActors(orchestrator, w1, wendyId);
  await navigateInApp(page, `/o/${org}/audit?task=${a1}&action=read`);
  const actor = page.locator('[data-testid="audit.row.actor"][data-actor-kind="subagent"]').first();
  await expect(actor).toContainText('#1.1', { timeout: 30_000 });
  await expect(actor).toContainText(RENAMED);
  await expect(actor).toContainText(ADA_NAME);

  await expectNoPlaintext(orchestrator, CANARIES);
});
