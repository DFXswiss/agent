// E2E-51 (11-agent-access.md section 5.5, ADR-068, rule E-27; KOR-62): an AI agent alone. Not a single browser step
// and no passkey for the agents: the operator's platform invitation goes to the agent tool on stdin; the agent creates
// its account (`register --own-account`: its private organization, no passkey, no browser session), deposits its own
// model access (`credentials set-api-key`, `settings set`), assigns the platform device, creates a task and reads the
// answer, founds a team organization and invites a second agent, which registers alone the same way and joins; then
// the first agent moves to a new computer with its recovery code (`recover`: the old computer is disconnected, the
// new one reads the task). Mixed: a person (Ada, in her browser with her passkey) invites the second agent into her
// organization; the agent joins with `org join`, Ada sees it on her members screen with its name, and the agent sees
// Ada in its member list. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  inviteMember,
  memberRow,
  navigate,
  personOf,
} from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { agentTool, agentToolRun } from '../fixtures/agent-tool.ts';
import { CLAUDE_MODEL, loadModelScenario, testApiKey } from '../fixtures/model.ts';
import { invite, platformDevice, poolReady, uuid } from '../fixtures/platform.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-51-agent-alone.json', import.meta.url), 'utf8'),
);
const BOT = 'Research Bot kz62-bot-r5t7y9';
const SECOND = 'Helper Bot kz62-helper-u2i4o6';
const TEAM = 'Robots kz62-team-a8s0d2';
const PROMPT = 'E2E51-ALONE kz62-prompt-f3g5h7: summarise the plan';
const ANSWER = 'Answer for the agent alone kz62-answer-p4l8m2.';
const CANARIES = ['kz62-team-a8s0d2', 'kz62-prompt-f3g5h7', 'kz62-answer-p4l8m2'];

type Json = Record<string, unknown>;

/** `register --own-account` with the invitation link on stdin, on the computer `home` (three anonymous requests). */
async function registerAlone(orchestrator: Orchestrator, home: string, link: string, name: string): Promise<Json> {
  await anonymousBudget(3 * COST.lookup);
  const args = ['register', '--own-account', '--display-name', name, '--name', 'Claude Code', '--host', home];
  const made = await agentTool(orchestrator, home, args, 120_000, link);
  expect(made).toMatchObject({ ok: true, display_name: name, secret_fields: ['recovery_code'] });
  return made;
}

/** No passkey, no browser session: the account of an agent alone. */
async function expectNoPerson(orchestrator: Orchestrator, user: string): Promise<void> {
  const rows = await orchestrator.hubQuery<{ passkeys: number; sessions: number; agents: number }>(
    `SELECT (SELECT count(*)::int FROM hub.passkey WHERE user_id = '${uuid(user)}') AS passkeys,
            (SELECT count(*)::int FROM hub.browser_session WHERE user_id = '${uuid(user)}') AS sessions,
            (SELECT count(*)::int FROM hub.agent WHERE user_id = '${uuid(user)}' AND state = 'active') AS agents`,
  );
  expect(rows).toEqual([{ passkeys: 0, sessions: 0, agents: 1 }]);
}

async function followUntil(orchestrator: Orchestrator, home: string, task: string, text: string): Promise<Json[]> {
  const seen: Json[] = [];
  let after = 0;
  const deadline = Date.now() + 240_000;
  while (Date.now() < deadline) {
    const args = ['task', 'follow', '--task', task, '--after', String(after), '--wait', '30'];
    const page = await agentTool(orchestrator, home, args, 90_000);
    seen.push(...(page.events as Json[]));
    after = page.next_after as number;
    if (seen.some((event) => JSON.stringify(event).includes(text))) return seen;
  }
  throw new Error(`task ${task}: no event with ${text} within 240 s`);
}

test('@E2E-51 @R-1.14 @needs-vm an agent alone registers, works, invites an agent and moves without any person', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_200_000);
  const apiKey = testApiKey();
  await loadModelScenario(orchestrator, scenario, { kind: 'api_key', token: apiKey });
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator, 1);

  // 1. The operator's platform invitation goes to the agent on stdin; the agent creates its account alone.
  const invitation = await invite(orchestrator, 'user');
  const made = await registerAlone(orchestrator, 'bot', `${e2e.hubOrigin}/invite#${invitation.code}`, BOT);
  expect(made).toMatchObject({ scope: 'platform', hub: e2e.hubOrigin });
  const user = made.user_id as string;
  const recoveryCode = made.recovery_code as string;
  await expectNoPerson(orchestrator, user);
  const audit = await orchestrator.hubQuery<{ action: string }>(
    `SELECT action FROM hub.audit WHERE actor_id = '${uuid(user)}' AND action IN ('agent.register', 'invite.register')
      ORDER BY seq`,
  );
  expect(audit.map((row) => row.action)).toEqual(['agent.register', 'invite.register']);
  const status = await agentTool(orchestrator, 'bot', ['status']);
  expect(status).toMatchObject({ user_id: user, agent_id: made.agent_id });
  const privateOrg = made.org_id as string;
  const defaultWg = made.default_wg as string;

  // 2. Its own model access, the platform device, a task and the answer.
  await agentTool(orchestrator, 'bot', ['credentials', 'set-api-key'], 60_000, apiKey);
  await agentTool(orchestrator, 'bot', ['settings', 'set', '--model-access', 'api_key', '--model', CLAUDE_MODEL]);
  expect(await agentTool(orchestrator, 'bot', ['device', 'assign', '--wg', defaultWg])).toMatchObject({
    org_id: privateOrg,
    device_id: device,
  });
  const task = await agentTool(orchestrator, 'bot', ['task', 'create', '--wg', defaultWg, '--text', PROMPT], 120_000);
  const taskId = task.task_id as string;
  const events = await followUntil(orchestrator, 'bot', taskId, ANSWER);
  expect(events.some((event) => event.kind === 'session.output' && JSON.stringify(event).includes(ANSWER))).toBe(true);
  expect(await orchestrator.doubleState('model')).toMatchObject({ ok: true });

  // 3. A team organization; the second agent registers alone through the org invitation and joins.
  const team = (await agentTool(orchestrator, 'bot', ['org', 'create', '--name', TEAM])).org_id as string;
  const invited = await agentTool(orchestrator, 'bot', ['org', 'invite', '--org', team]);
  const second = await registerAlone(orchestrator, 'helper', invited.invite_link as string, SECOND);
  expect(second).toMatchObject({ scope: 'org', org_id: team, role: 'member' });
  await expectNoPerson(orchestrator, second.user_id as string);
  const members = JSON.stringify(await agentTool(orchestrator, 'bot', ['member', 'list', '--org', team]));
  expect(members).toContain(SECOND);
  expect(members).toContain(second.user_id as string);

  // 4. The first agent moves to a new computer with its recovery code; the old computer is disconnected.
  await anonymousBudget(3 * COST.lookup);
  const moved = await agentTool(orchestrator, 'botnew', ['recover', '--hub', e2e.hubOrigin], 120_000, recoveryCode);
  expect(moved).toMatchObject({ ok: true, user_id: user, secret_fields: ['recovery_code'] });
  expect(moved.agent_id).not.toBe(made.agent_id);
  expect(moved.recovery_code).not.toBe(recoveryCode);
  expect((await agentToolRun(orchestrator, 'bot', ['status'])).result).toMatchObject({
    ok: false,
    error: 'agent_revoked',
  });
  const shown = await agentTool(orchestrator, 'botnew', ['task', 'show', '--task', taskId]);
  expect(JSON.stringify(shown.events)).toContain('kz62-answer-p4l8m2');
  await expectNoPerson(orchestrator, user);

  // 5. Mixed: Ada invites the second agent in her browser; it joins, she sees its name, it sees hers.
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Person');
  const adaOrg = ada.onboarded.org;
  const code = await inviteMember(page, adaOrg);
  const joined = await agentTool(
    orchestrator,
    'helper',
    ['org', 'join', '--display-name', SECOND],
    60_000,
    `${e2e.hubOrigin}/invite#${code}`,
  );
  expect(joined).toMatchObject({ org_id: adaOrg });
  await navigate(page, `/o/${adaOrg}/members`);
  await expect(memberRow(page, SECOND)).toBeVisible({ timeout: 30_000 });
  expect(JSON.stringify(await agentTool(orchestrator, 'helper', ['member', 'list', '--org', adaOrg]))).toContain(
    'Ada Person',
  );

  await expectNoPlaintext(orchestrator, CANARIES);
  await closingChecks(orchestrator, true);
});
