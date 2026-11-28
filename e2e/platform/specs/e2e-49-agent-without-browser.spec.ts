// E2E-49 (11-agent-access.md sections 5.3, 6 and 8.4; KOR-42 goal 4): the agent works without a browser. Ada pairs
// her agent once (loopback `pair`, confirmed with her passkey on `/connect`); from then on only the agent tool acts:
// it creates a team organization, invites Bob (`org invite`; Bob registers through the link in his own browser),
// creates a workgroup and grants Bob read access, assigns the platform device, creates a task with a prompt and
// follows it to the answer (`task follow`), sends a further prompt, interrupts the session's turn while the model is
// still working, stops the session, closes the task and writes a workgroup whiteboard entry. Bob sees the org, the
// task with its chat and the entry in the web. Then the agent removes Bob (`member remove`: `remove` plus the
// rotations of every scope he could read) and writes a new entry: Bob's keys still open the old entry, as documented,
// and none of them opens the new one. Finally the agent cannot confirm a pairing (`POST /v1/me/agents` is
// `403 forbidden` for the agent of an account with a passkey) and revokes itself (`agents revoke`): its state is gone
// (`not_paired`) and a copy of it that still holds the key is answered `agent_revoked`. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  closingChecks,
  joinAsNewUser,
  memberLog,
  navigate,
  openScopeEvents,
  personOf,
  refusal,
  visit,
} from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { agentTool, agentToolRun, copyAgentHome, pairAgent } from '../fixtures/agent-tool.ts';
import { expectAnswerInChat, openChat, operator, platformDevice, poolReady, uuid } from '../fixtures/platform.ts';
import { ORG, WG } from '../../../web/src/core/log/state';
import type { Orchestrator } from '../fixtures/orchestrator.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-49-agent-without-browser.json', import.meta.url), 'utf8'),
);
const ORG_NAME = 'Agent team kz46-org-a1s3d5';
const WG_NAME = 'Agent work kz46-wg-f7g9h1';
const FIRST_PROMPT = 'E2E46-FIRST kz46-prompt-q1w3e5: hello from the agent';
const FURTHER_PROMPT = 'E2E46-FURTHER kz46-prompt-r7t9y1: take your time';
const ANSWER = 'Agent task answer kz46-answer-h3j5k7.';
const OLD_ENTRY = 'Before the removal kz46-old-z2x4c6';
const NEW_ENTRY = 'After the removal kz46-new-v8b0n2';
const CANARIES = [
  'kz46-org-a1s3d5',
  'kz46-wg-f7g9h1',
  'kz46-prompt-q1w3e5',
  'kz46-prompt-r7t9y1',
  'kz46-answer-h3j5k7',
  'kz46-old-z2x4c6',
  'kz46-new-v8b0n2',
];
const HOME = 'ada';

type Json = Record<string, unknown>;

/** `task follow` in a loop (each call returns `next_after`) until an event contains `text`; returns the events. */
async function followUntil(orchestrator: Orchestrator, task: string, text: string): Promise<Json[]> {
  const seen: Json[] = [];
  let after = 0;
  const deadline = Date.now() + 240_000;
  while (Date.now() < deadline) {
    const page = await agentTool(
      orchestrator,
      HOME,
      ['task', 'follow', '--task', task, '--after', String(after), '--wait', '30'],
      90_000,
    );
    expect(page).toMatchObject({ ok: true, task_id: task });
    seen.push(...(page.events as Json[]));
    after = page.next_after as number;
    if (seen.some((event) => JSON.stringify(event).includes(text))) return seen;
  }
  throw new Error(`task ${task}: no event with ${text} within 240 s`);
}

const sessionState = async (orchestrator: Orchestrator, session: string) =>
  ((await agentTool(orchestrator, HOME, ['session', 'show', '--session', session])).session as Json).state;

test('@E2E-49 @needs-vm the agent sets up a team, runs a task and removes a member with the agent tool alone', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_200_000);
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator, 1);

  // 1. Ada pairs her agent once; the page is not used for anything the agent does after this.
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Agent-Owner');
  const paired = await pairAgent(orchestrator, page, e2e.hubOrigin, HOME);
  expect(paired).toMatchObject({ ok: true });
  expect(await agentTool(orchestrator, HOME, ['status'])).toMatchObject({ user_id: ada.user.userId });

  // 2. A team organization (the operator grants it platform access, ADR-026).
  const created = await agentTool(orchestrator, HOME, ['org', 'create', '--name', ORG_NAME]);
  const org = created.org_id as string;
  const defaultWg = created.default_wg as string;
  expect(org).not.toBe(ada.onboarded.org);
  expect(await operator(orchestrator, ['grant-org-access', '--org', org])).toContain('appended org_platform_access');
  const shown = await agentTool(orchestrator, HOME, ['org', 'show', '--org', org]);
  expect(JSON.stringify(shown)).toContain(ORG_NAME);

  // 3. Bob is invited by the agent and registers through the link in his own browser.
  const invited = await agentTool(orchestrator, HOME, ['org', 'invite', '--org', org, '--role', 'member']);
  expect(invited).toMatchObject({ role: 'member', secret_fields: ['invite_link'] });
  const link = invited.invite_link as string;
  expect(link.startsWith(`${e2e.hubOrigin}/invite#`), link).toBe(true);
  const bob = await joinAsNewUser(browser, e2e, link.split('#')[1]!, 'Bob Member');
  expect(bob.org).toBe(org);
  const members = await agentTool(orchestrator, HOME, ['member', 'list', '--org', org]);
  expect(JSON.stringify(members)).toContain(bob.user.userId);

  // 4. A workgroup, Bob's read access to it and the platform device.
  const wgCreated = await agentTool(orchestrator, HOME, ['wg', 'create', '--org', org, '--name', WG_NAME]);
  const wg = wgCreated.wg_id as string;
  expect(wgCreated).toMatchObject({ org_id: org, name: WG_NAME });
  expect(
    await agentTool(orchestrator, HOME, ['wg', 'grant', '--wg', wg, '--user', bob.user.userId, '--access', 'read']),
  ).toMatchObject({ org_id: org, wg_id: wg, user_id: bob.user.userId, access: 'read' });
  expect(await agentTool(orchestrator, HOME, ['device', 'assign', '--wg', wg])).toMatchObject({
    org_id: org,
    wg_id: wg,
    device_id: device,
  });
  const log = await ada.user.log(org);
  expect(log.state.wgAccess(bob.user.userId, wg)).toBe('read');
  expect(log.state.assigned(device, wg)).toBe(true);
  expect(log.state.member(bob.user.userId)).toMatchObject({ role: 'member' });

  // 5. A task with a prompt, followed to the answer.
  const task = await agentTool(orchestrator, HOME, ['task', 'create', '--wg', wg, '--text', FIRST_PROMPT], 120_000);
  expect(task).toMatchObject({ org_id: org, wg_id: wg });
  const taskId = task.task_id as string;
  const events = await followUntil(orchestrator, taskId, ANSWER);
  expect(events.find((event) => event.kind === 'prompt')).toMatchObject({ body: { text: FIRST_PROMPT } });
  expect(events.some((event) => event.kind === 'session.output' && JSON.stringify(event).includes(ANSWER))).toBe(true);
  const listed = await agentTool(orchestrator, HOME, ['task', 'list', '--wg', wg]);
  expect((listed.tasks as Json[]).map((row) => row.task_id)).toEqual([taskId]);

  // 6. A further prompt; the agent interrupts the turn while the model still works on it, then stops the session.
  const further = await agentTool(orchestrator, HOME, ['task', 'prompt', '--task', taskId, '--text', FURTHER_PROMPT]);
  const session = (further.session_id ?? task.session_id) as string;
  expect(session).toMatch(/^[0-9a-f-]{36}$/);
  await expect
    .poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 180_000, intervals: [1000] })
    .toBe(true); // the further prompt reached the model (every step of the scenario consumed)
  await expect.poll(() => sessionState(orchestrator, session), { timeout: 60_000, intervals: [1000] }).toBe('working');
  const interrupted = Date.now();
  expect(await agentTool(orchestrator, HOME, ['session', 'interrupt', '--session', session])).toMatchObject({
    session_id: session,
    action: 'interrupt',
  });
  await expect.poll(() => sessionState(orchestrator, session), { timeout: 60_000, intervals: [1000] }).toBe('idle');
  expect(Date.now() - interrupted).toBeLessThan(90_000); // well before the delayed answer
  expect(await agentTool(orchestrator, HOME, ['session', 'stop', '--session', session])).toMatchObject({
    session_id: session,
    action: 'stop',
  });
  await expect.poll(() => sessionState(orchestrator, session), { timeout: 90_000, intervals: [1000] }).toBe('stopped');
  expect(await agentTool(orchestrator, HOME, ['task', 'close', '--task', taskId])).toMatchObject({
    task: { task_id: taskId, state: 'closed' },
  });
  const chat = await agentTool(orchestrator, HOME, ['task', 'show', '--task', taskId]);
  expect(chat).toMatchObject({ task: { task_id: taskId, state: 'closed' } });
  expect(JSON.stringify(chat.events)).toContain('kz46-prompt-r7t9y1');
  expect(JSON.stringify(chat.events)).not.toContain('kz46-late-w2e4r6');

  // 7. A whiteboard entry on the workgroup level.
  const entry = await agentTool(orchestrator, HOME, [
    'whiteboard',
    'add',
    '--org',
    org,
    '--wg',
    wg,
    '--text',
    OLD_ENTRY,
  ]);
  expect(entry).toMatchObject({ level: 'workgroup', org_id: org, wg_id: wg });

  // 8. Bob sees all of it in the web: the org, the task with its chat, the entry.
  await visit(bob.page, `/o/${org}`);
  await bob.page.getByTestId('org-switcher.current').click();
  await expect(bob.page.locator(`[data-testid="org-switcher.item"][data-org="${uuid(org)}"]`)).toBeVisible();
  await bob.page.keyboard.press('Escape');
  await navigate(bob.page, `/o/${org}/t/${taskId}`);
  await openChat(bob.page); // the chat is the second tab (KONZEPT E-43)
  await expectAnswerInChat(bob.page, ANSWER);
  await expect(
    bob.page.locator('[data-testid="chat.item"][data-kind="prompt"]').filter({ hasText: 'kz46-prompt-q1w3e5' }),
  ).toBeVisible();
  await navigate(bob.page, `/o/${org}/w/${wg}/whiteboard`);
  await expect(bob.page.getByTestId('whiteboard.item').filter({ hasText: OLD_ENTRY })).toBeVisible({
    timeout: 30_000,
  });
  const bobKeys = await bob.user.keys(org, await memberLog(bob.user, org));

  // 9. The agent removes Bob: one batch with `remove` and a rotation of every scope he could read.
  const before = await ada.user.log(org);
  const removed = await agentTool(orchestrator, HOME, ['member', 'remove', '--org', org, '--user', bob.user.userId]);
  expect(removed).toMatchObject({ ok: true, user_id: bob.user.userId });
  const after = await ada.user.log(org);
  const batch = after.entries.slice(before.entries.length).map((item) => item.body);
  expect(batch[0]).toMatchObject({ type: 'remove', subject: bob.user.userId });
  expect(batch.length).toBeGreaterThan(1);
  expect(batch.slice(1).every((body) => body.type === 'rotate')).toBe(true);
  expect(after.state.member(bob.user.userId)).toBeNull();
  expect([after.state.epoch(ORG, org), after.state.epoch(WG, defaultWg), after.state.epoch(WG, wg)]).toEqual([2, 2, 2]);
  expect(await refusal(bob.user, 'GET', `/v1/orgs/${org}`)).toBe('404 not_found');

  // 10. A new entry: Bob's keys open the old one (what he already read stays with him), none opens the new one.
  await agentTool(orchestrator, HOME, ['whiteboard', 'add', '--org', org, '--wg', wg, '--text', NEW_ENTRY]);
  const board = await agentTool(orchestrator, HOME, ['whiteboard', 'list', '--org', org, '--wg', wg]);
  for (const text of [OLD_ENTRY, NEW_ENTRY]) expect(JSON.stringify(board.items)).toContain(text);
  const bobView = await openScopeEvents(orchestrator, ada.user, { kind: WG, id: wg }, bobKeys, ['whiteboard.entry']);
  expect(bobView.opened.join('\n')).toContain('kz46-old-z2x4c6');
  expect(bobView.opened.join('\n')).not.toContain('kz46-new-v8b0n2');
  expect(bobView.locked).toEqual([2]);
  const wraps = await orchestrator.hubQuery<{ recipient_id: string }>(
    `SELECT recipient_id::text FROM hub.key_wrap WHERE epoch = 2 AND scope_id = '${uuid(wg)}'`,
  );
  expect(wraps.map((row) => row.recipient_id)).not.toContain(bob.user.userId);
  await bob.context.close();

  // 11. The agent cannot confirm a pairing: Ada's account has a passkey, so `POST /v1/me/agents` needs her passkey
  // session (KOR-62: only the agent of an account without a passkey confirms its successor itself).
  const confirm = await agentToolRun(orchestrator, HOME, [
    'api',
    'POST',
    '/v1/me/agents',
    '--body',
    JSON.stringify({
      pairing_id: '0190f0d0-0000-7000-8000-000000000046',
      bundle_ct: 'A'.repeat(64),
      label_ct: 'A'.repeat(64),
      claim_hash: 'A'.repeat(43),
    }),
  ]);
  expect(confirm.rc).toBe(1);
  expect(confirm.result).toMatchObject({ ok: false, error: 'hub_refused', hub_error: 'forbidden', status: 403 });

  // 12. The agent revokes itself: its state is gone, a copy that still holds the key is refused (`agent_revoked`).
  const agents = await agentTool(orchestrator, HOME, ['agents', 'list']);
  const self = (agents.agents as Json[]).find((row) => row.this === true)!;
  expect(self).toMatchObject({ state: 'active' });
  await copyAgentHome(orchestrator, HOME, 'ada_copy');
  expect(await agentTool(orchestrator, HOME, ['agents', 'revoke', '--id', self.agent_id as string])).toMatchObject({
    revoked: 1,
    this_agent_revoked: true,
  });
  expect(
    await orchestrator.hubQuery<{ state: string }>(
      `SELECT state FROM hub.agent WHERE id = '${uuid(self.agent_id as string)}'`,
    ),
  ).toEqual([{ state: 'revoked' }]);
  expect((await agentToolRun(orchestrator, HOME, ['org', 'list'])).result).toMatchObject({
    ok: false,
    error: 'not_paired',
  });
  expect((await agentToolRun(orchestrator, 'ada_copy', ['org', 'list'])).result).toMatchObject({
    ok: false,
    error: 'agent_revoked',
  });

  await expectNoPlaintext(orchestrator, CANARIES);
  await closingChecks(orchestrator, true);
});
