// E2E-61 (ADR-079, 15-prerequisites.md, rules E-05 and E-11; KOR-49): no approval as a precondition. The operator
// turns the root-signed switch `open_platform_access` with `agent-hub platform set-policy`: while it is off, a new team
// organization's task waits `platform_access_required` and the user with the platform role `operator` gets one
// `operator.request`; while it is on, the admin's `agent-platform task create` assigns the platform device itself and
// the task runs at once with the platform model access (`auto`, no credential of its own). The operator's subscription
// pool of a runner serves an organization from the moment `grant-operator-subscriptions` is in the verified platform
// log and stops after the revocation - without a runner restart. A person with an explicit own-access preference and
// no credential: the start fails `model_access_unavailable`, the chat shows the card that starts the connection, and
// after the API key is stored the task continues by itself. A push to `main` goes through when the workgroup lists no
// protected branch (GitHub double); once it lists `main` a commit there is refused `branch_protected`. A switch entry
// not signed by the root is refused (`log_signature`) and the runner keeps the verified log. A private organization
// without a platform invitation stays refused (`platform_invite_required`) also with `open_platform_invites` on. With
// billing active and credit 0 the task waits `credit_exhausted` and the operator is asked. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  enrollRunner,
  expectDevice,
  navigate,
  removeRunner,
  spawnRunner,
} from '../fixtures/identity.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import { agentTool } from '../fixtures/agent-tool.ts';
import {
  chooseModelAccess,
  connectedDevices,
  devicesOnline,
  failureReason,
  storeApiKey,
  testApiKey,
} from '../fixtures/model.ts';
import { connectGitHub, githubRepo, hubOperations, repository, seedGitHub, worldOf } from '../fixtures/github.ts';
import {
  anonymous,
  expectAnswerInChat,
  home,
  invite,
  mainSessions,
  onboard,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { rootRequest } from '../../../web/e2e/helpers/platform.ts';
import { preparePrivateOrg } from '../../../web/src/core/account/genesis';
import { ed25519KeyPair } from '../../../web/src/core/crypto/keys';
import { random } from '../../../web/src/core/crypto/subtle';
import { buildPlatformEntries } from '../../../web/src/core/log/entries';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-61-no-approval.json', import.meta.url), 'utf8'),
);
const PRICES = JSON.parse(
  readFileSync(new URL('../../../docs/platform/billing/price-list-2026-11-24.json', import.meta.url), 'utf8'),
) as Record<string, unknown>;
/** Without `valid_from`: a list applies from the moment it is added. */
const { valid_from: _validFrom, ...NOW_PRICES } = PRICES;

const OPEN_ACCESS = 'open_platform_access';
const OPEN_INVITES = 'open_platform_invites';
const LOGIN = 'kz59-octo-r8w2';
const HELLO = 'octo-org/hello';
const TEAM_OFF = 'Closed kz59-team-off-m1x4';
const TEAM_ON = 'Open kz59-team-on-n2y5';
const TEAM_SUB = 'Pool kz59-team-sub-p3z6';
const PROMPT = {
  off: 'E2E59-OFF kz59-prompt-off-q4a7: hello',
  open: 'E2E59-OPEN kz59-prompt-open-r5b8: hello',
  before: 'E2E59-SUB-BEFORE kz59-prompt-before-s6c9: hello',
  granted: 'E2E59-SUB-GRANTED kz59-prompt-granted-t7d1: hello',
  revoked: 'E2E59-SUB-REVOKED kz59-prompt-revoked-u8e2: hello',
  own: 'E2E59-OWN kz59-prompt-own-v9f3: hello',
  push: 'E2E59-PUSH kz59-prompt-push-w1g4: write the notes and push them to main',
  protectedMain: 'E2E59-PROTECTED kz59-prompt-protected-x2h5: change the notes on main',
  credit: 'E2E59-CREDIT kz59-prompt-credit-y3j6: hello',
};
const CANARIES = [
  'kz59-team-off-m1x4',
  'kz59-team-on-n2y5',
  'kz59-team-sub-p3z6',
  'kz59-prompt-off-q4a7',
  'kz59-prompt-open-r5b8',
  'kz59-prompt-before-s6c9',
  'kz59-prompt-granted-t7d1',
  'kz59-prompt-revoked-u8e2',
  'kz59-prompt-own-v9f3',
  'kz59-prompt-push-w1g4',
  'kz59-prompt-protected-x2h5',
  'kz59-prompt-credit-y3j6',
  'kz59-open-a3f7',
  'kz59-before-b4g8',
  'kz59-granted-c5h9',
  'kz59-revoked-d6j1',
  'kz59-own-e7k2',
  'kz59-pushed-f8m3',
  'kz59-protected-g9n4',
  'kz59-name-h1p5',
];

type Json = Record<string, unknown>;

interface Placed {
  readonly id: string;
  readonly device_id: string | null;
  readonly state: string;
  readonly state_reason: string | null;
  readonly model_access: string | null;
}

/** `register --own-account` with the operator's platform invitation on stdin (three anonymous requests). */
async function registerAlone(orchestrator: Orchestrator, origin: string, home: string, name: string): Promise<Json> {
  const invitation = await invite(orchestrator, 'user');
  await anonymousBudget(3 * COST.lookup);
  const args = ['register', '--own-account', '--display-name', name, '--name', 'Claude Code', '--host', home];
  const made = await agentTool(orchestrator, home, args, 120_000, `${origin}/invite#${invitation.code}`);
  expect(made).toMatchObject({ ok: true, display_name: name });
  return made;
}

/** The task's events through `task follow` until one contains `text`. */
async function followUntil(orchestrator: Orchestrator, home: string, task: string, text: string): Promise<void> {
  let after = 0;
  const deadline = Date.now() + 240_000;
  while (Date.now() < deadline) {
    const args = ['task', 'follow', '--task', task, '--after', String(after), '--wait', '30'];
    const page = await agentTool(orchestrator, home, args, 90_000);
    if (JSON.stringify(page.events).includes(text)) return;
    after = page.next_after as number;
  }
  throw new Error(`task ${task}: no event with ${text} within 240 s`);
}

/** The newest main session of a task with its access kind (null while it has none). */
async function latestSession(orchestrator: Orchestrator, task: string): Promise<Placed | null> {
  const [row] = await orchestrator.hubQuery<Placed>(
    `SELECT id::text, device_id::text, state, state_reason, model_access FROM hub.session
      WHERE task_id = '${uuid(task)}' AND depth = 0 ORDER BY created_at DESC, id DESC LIMIT 1`,
  );
  return row ?? null;
}

const taskState = async (orchestrator: Orchestrator, task: string) =>
  (
    await orchestrator.hubQuery<{ state: string; state_reason: string | null }>(
      `SELECT state, state_reason FROM hub.task WHERE id = '${uuid(task)}'`,
    )
  )[0];

/** The refs of the operator's `operator.request` notifications (account level: no organization), oldest first. */
const operatorRequests = async (orchestrator: Orchestrator, user: string) =>
  (
    await orchestrator.hubQuery<{ ref: string; org: string | null }>(
      `SELECT ref, org_id::text AS org FROM hub.notification
        WHERE recipient_id = '${uuid(user)}' AND kind = 'operator.request' ORDER BY created_at`,
    )
  ).map((row) => `${row.ref}${row.org === null ? '' : ` org ${row.org}`}`);

const hubHead = async (orchestrator: Orchestrator) =>
  (await orchestrator.hubQuery<{ seq: number }>('SELECT max(seq)::int AS seq FROM hub.platform_entry'))[0]!.seq;

/** The newest platform-log entry the runner stored after verifying it (`orgs.book`). */
const runnerHead = async (orchestrator: Orchestrator, runner: string) =>
  (
    await runnerStore<{ seq: number | null }>(
      orchestrator,
      'SELECT max(seq)::int AS seq FROM runner.platform_log_entry',
      runner,
    )
  )[0]?.seq ?? -1;

/** Waits until the runner verified the hub's whole platform log. */
async function runnerCaughtUp(orchestrator: Orchestrator, runner: string): Promise<void> {
  const head = await hubHead(orchestrator);
  await expect.poll(() => runnerHead(orchestrator, runner), { timeout: 60_000, intervals: [500] }).toBe(head);
}

/** `agent-hub platform set-policy NAME on|off`. */
async function setPolicy(orchestrator: Orchestrator, name: string, on: boolean): Promise<void> {
  expect(await operator(orchestrator, ['set-policy', name, on ? 'on' : 'off'])).toMatch(
    /^appended platform_policy \(seq \d+\)$/m,
  );
}

interface Billing {
  readonly rc: number;
  readonly lines: Json[];
  readonly stderr: string;
}

const billing = (orchestrator: Orchestrator, args: string[], priceList?: unknown) =>
  orchestrator.call<Billing>(
    'POST',
    '/hub/billing',
    priceList === undefined ? { args } : { args, price_list: priceList },
  );

/** Billing on as in the runbook (rate, start credit, price list) unless an earlier spec of the run did it. */
async function billingActive(orchestrator: Orchestrator): Promise<void> {
  const [inForce] = await orchestrator.hubQuery<{ n: number }>(
    'SELECT count(*)::int AS n FROM hub.price_list WHERE valid_from <= now()',
  );
  if (inForce!.n > 0) return;
  const [today] = await orchestrator.hubQuery<{ d: string }>(
    "SELECT to_char(now() AT TIME ZONE 'Europe/Zurich', 'YYYY-MM-DD') AS d",
  );
  const rate = await billing(orchestrator, ['fx', 'set', '--day', today!.d, '--rate', '0.8486', '--source', 'BAZG']);
  expect(rate.rc, rate.stderr).toBe(0);
  // a start credit as E2E-46 sets it: organizations of later specs (and of a retry) are not exhausted at once
  const before = await connectedDevices(orchestrator);
  const [now] = await orchestrator.hubQuery<{ now: string }>('SELECT now()::text AS now');
  await orchestrator.call('POST', '/hub/start-credit', { usd: '5' });
  await devicesOnline(orchestrator, before, now!.now);
  const added = await billing(orchestrator, ['price-list', 'add', '--file', '@price_list'], NOW_PRICES);
  expect(added.rc, added.stderr).toBe(0);
}

test.describe.configure({ mode: 'serial' });

test('@E2E-61 @needs-vm no approval as a precondition: switches, operator subscriptions, model access, branches and requests to the operator', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-61 runs on T1');
  test.setTimeout(2_400_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, 'octo-org', [repository(HELLO)]));
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator, 1);
  let spawned: string | null = null;
  const closeLater: [string, string][] = [];
  try {
    // Both switches start off (an explicit entry: the request rule's answer is any later switch entry).
    await setPolicy(orchestrator, OPEN_ACCESS, false);
    await setPolicy(orchestrator, OPEN_INVITES, false);
    expect(await operator(orchestrator, ['settings'])).toMatch(
      /^policy open_platform_access off\npolicy open_platform_invites off$/m,
    );

    // The operator: an account with the platform role `operator`; the founder of the team organizations.
    const opal = (await registerAlone(orchestrator, e2e.hubOrigin, 'opal', 'Opal Operator')).user_id as string;
    expect(await operator(orchestrator, ['grant-role', 'operator', '--user', opal])).toMatch(/^appended role_grant/m);
    expect(await agentTool(orchestrator, 'opal', ['platform', 'settings'])).toMatchObject({ roles: ['operator'] });
    await registerAlone(orchestrator, e2e.hubOrigin, 'founder', 'Fritz Founder');

    // 1a. Switch off: the team organization has no platform access; its task waits and the operator is asked.
    const off = await agentTool(orchestrator, 'founder', ['org', 'create', '--name', TEAM_OFF]);
    const offOrg = off.org_id as string;
    const offTask = await agentTool(
      orchestrator,
      'founder',
      ['task', 'create', '--wg', off.default_wg as string, '--text', PROMPT.off],
      120_000,
    );
    expect(offTask.assigned_device).toBeNull();
    closeLater.push(['founder', offTask.task_id as string]);
    await expect
      .poll(() => taskState(orchestrator, offTask.task_id as string), { timeout: 60_000 })
      .toEqual({ state: 'scheduling', state_reason: 'platform_access_required' });
    await expect
      .poll(() => operatorRequests(orchestrator, opal), { timeout: 60_000 })
      .toEqual([`platform_access_required:${offOrg}`]);
    expect(JSON.stringify(await agentTool(orchestrator, 'opal', ['notifications', 'list']))).toContain(
      'operator.request',
    );
    expect(await operator(orchestrator, ['waiting'])).toMatch(
      new RegExp(`^org ${offOrg} wg \\S+ platform_access_required tasks 1 since \\S+ platform access no$`, 'm'),
    );

    // 1b. Switch on: a new team organization works at once; the admin's task assigns the platform device itself and
    // runs with the platform model access (preference `auto`, no credential of its own).
    await setPolicy(orchestrator, OPEN_ACCESS, true);
    expect(await operator(orchestrator, ['settings'])).toMatch(/^policy open_platform_access on$/m);
    expect(await agentTool(orchestrator, 'founder', ['platform', 'settings'])).toMatchObject({
      policies: { [OPEN_ACCESS]: true, [OPEN_INVITES]: false },
    });
    await runnerCaughtUp(orchestrator, 'runner-1');
    const on = await agentTool(orchestrator, 'founder', ['org', 'create', '--name', TEAM_ON]);
    const onOrg = on.org_id as string;
    const onWg = on.default_wg as string;
    const [access] = await orchestrator.hubQuery<{ private: boolean; platform_access: boolean }>(
      `SELECT private, platform_access FROM hub.org WHERE id = '${uuid(onOrg)}'`,
    );
    expect(access).toEqual({ private: false, platform_access: true });
    const openTask = await agentTool(
      orchestrator,
      'founder',
      ['task', 'create', '--wg', onWg, '--text', PROMPT.open],
      120_000,
    );
    expect(openTask.assigned_device).toBe(device);
    await followUntil(orchestrator, 'founder', openTask.task_id as string, 'kz59-open-a3f7');
    expect(await latestSession(orchestrator, openTask.task_id as string)).toMatchObject({
      device_id: device,
      model_access: 'platform',
    });
    await agentTool(orchestrator, 'founder', ['task', 'close', '--task', openTask.task_id as string]);

    // 2. The operator's subscriptions: a further platform device with a pool; the list lives in the platform log and
    // takes effect without a restart of that runner (grant, then revocation).
    spawned = (await spawnRunner(orchestrator, { pool_size: 1, memory_reserve_mib: 0, operator_subscriptions: true }))
      .name;
    const pooled = await enrollRunner(orchestrator, spawned, (await invite(orchestrator, 'device')).code, true);
    expect(await operator(orchestrator, ['certify-pending', '--device', pooled])).toContain(`certified ${pooled}`);
    await expectDevice(orchestrator, pooled, { state: 'active', online: true });
    await poolReady(orchestrator, 1);
    const sub = await agentTool(orchestrator, 'founder', ['org', 'create', '--name', TEAM_SUB]);
    const subOrg = sub.org_id as string;
    const subWg = sub.default_wg as string;
    expect(
      await agentTool(orchestrator, 'founder', ['device', 'assign', '--org', subOrg, '--device', pooled]),
    ).toMatchObject({ org_id: subOrg, device_id: pooled });
    const runSub = async (prompt: string, answer: string): Promise<Placed> => {
      const created = await agentTool(
        orchestrator,
        'founder',
        ['task', 'create', '--wg', subWg, '--text', prompt],
        120_000,
      );
      expect(created.assigned_device).toBeNull(); // the organization's device is assigned already
      const task = created.task_id as string;
      await followUntil(orchestrator, 'founder', task, answer);
      const placed = (await latestSession(orchestrator, task))!;
      await agentTool(orchestrator, 'founder', ['task', 'close', '--task', task]);
      return placed;
    };
    await runnerCaughtUp(orchestrator, spawned);
    expect(await runSub(PROMPT.before, 'kz59-before-b4g8')).toMatchObject({
      device_id: pooled,
      model_access: 'platform',
    });
    expect(await operator(orchestrator, ['grant-operator-subscriptions', '--org', subOrg])).toMatch(
      /^appended org_operator_subscriptions \(seq \d+\)$/m,
    );
    expect(await operator(orchestrator, ['settings'])).toContain(`org ${subOrg} operator subscriptions granted`);
    expect(await agentTool(orchestrator, 'founder', ['platform', 'settings'])).toMatchObject({
      operator_subscriptions: [subOrg],
    });
    await runnerCaughtUp(orchestrator, spawned);
    expect(await runSub(PROMPT.granted, 'kz59-granted-c5h9')).toMatchObject({
      device_id: pooled,
      model_access: 'subscription',
    });
    expect(await operator(orchestrator, ['grant-operator-subscriptions', '--org', subOrg, '--revoke'])).toMatch(
      /^appended org_operator_subscriptions \(seq \d+\)$/m,
    );
    await runnerCaughtUp(orchestrator, spawned);
    expect(await runSub(PROMPT.revoked, 'kz59-revoked-d6j1')).toMatchObject({
      device_id: pooled,
      model_access: 'platform',
    });
    await removeRunner(orchestrator, spawned);
    spawned = null;

    // 3. A person with an explicit own-access preference and no credential: the start fails at once, the chat shows
    // the card that starts the connection, and the stored API key lets the task continue by itself.
    await authenticator('platform');
    await anonymousBudget(COST.register);
    const ada = await onboard(page, orchestrator, 'Ada kz59-name-h1p5');
    const adaUser = await PageUser.open(page, ada.recoveryCode);
    await chooseModelAccess(page, 'api_key');
    await home(page);
    const own = await sendPrompt(page, PROMPT.own);
    await expect.poll(() => failureReason(orchestrator, own), { timeout: 180_000 }).toBe('model_access_unavailable');
    const card = page.getByTestId('chat.model-access');
    await expect(card).toBeVisible({ timeout: 60_000 });
    await card.getByTestId('chat.model-access.connect').click();
    await storeApiKey(page, testApiKey());
    await expect.poll(async () => (await mainSessions(orchestrator, own)).length, { timeout: 60_000 }).toBe(2);
    await navigate(page, `/o/${ada.org}/t/${own}`);
    await expectAnswerInChat(page, 'Own access at last kz59-own-e7k2.');
    const [failed, next] = await mainSessions(orchestrator, own);
    expect((await latestSession(orchestrator, own))!.model_access).toBe('api_key');
    const [retry] = await orchestrator.hubQuery<{ reason: string | null }>(
      `SELECT reason FROM hub.session_transition WHERE session_id = '${uuid(next!.id)}' AND state = 'requested'`,
    );
    expect(retry).toEqual({ reason: 'model_access_unavailable' });
    expect(failed!.state).toBe('failed');
    await expect(card).toHaveCount(0);

    // 4. Protected branches: none without a workgroup list - a push to main goes through at the GitHub double.
    await connectGitHub(page, orchestrator, LOGIN);
    const refsBefore = (await githubRepo(orchestrator, HELLO)).refs;
    await home(page);
    const push = await sendPrompt(page, PROMPT.push);
    await expectAnswerInChat(page, 'Pushed to main kz59-pushed-f8m3.');
    expect((await hubOperations(orchestrator, push)).map((op) => [op.type, op.state, op.error_code])).toEqual([
      ['github.checkout', 'succeeded', null],
      ['github.commit', 'succeeded', null],
      ['github.push', 'succeeded', null],
    ]);
    expect((await githubRepo(orchestrator, HELLO)).refs).not.toEqual(refsBefore);
    // With `main` in the workgroup's list the commit there is refused before any GitHub call.
    const [adaOrg] = await orchestrator.hubQuery<{ default_wg: string }>(
      `SELECT default_wg::text FROM hub.org WHERE id = '${uuid(ada.org)}'`,
    );
    await navigate(page, `/o/${ada.org}/w/${adaOrg!.default_wg}/settings`);
    await expect(page.getByTestId('wg-settings.protected')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('wg-settings.protected')).toHaveValue('');
    await page.getByTestId('wg-settings.protected').fill('main');
    await page.getByTestId('wg-settings.save').click();
    await expect(page.getByTestId('wg-settings.saved')).toBeVisible();
    await home(page);
    const guarded = await sendPrompt(page, PROMPT.protectedMain);
    await expectAnswerInChat(page, 'Main is protected kz59-protected-g9n4.');
    expect((await hubOperations(orchestrator, guarded)).map((op) => [op.type, op.state, op.error_code])).toEqual([
      ['github.commit', 'rejected', 'branch_protected'],
    ]);

    // 5. A switch entry not signed by the root is refused; the log, the switch and the runner stay as they were.
    const [head] = await orchestrator.hubQuery<{ seq: number; hash: string }>(
      'SELECT seq, entry_hash AS hash FROM hub.platform_entry ORDER BY seq DESC LIMIT 1',
    );
    const foreign = await ed25519KeyPair(random(32));
    const forged = await buildPlatformEntries(head!, foreign.privateKey, [
      { type: 'platform_policy', name: OPEN_ACCESS, on: false },
    ]);
    const refused = await rootRequest(e2e.hubOrigin, 'POST', '/v1/platform/log', {
      expected_seq: head!.seq + 1,
      entries: forged,
    });
    expect({ status: refused.status, body: (await refused.json()) as Json }).toMatchObject({
      status: 400,
      body: { error: 'log_signature' },
    });
    expect(await hubHead(orchestrator)).toBe(head!.seq);
    expect(await operator(orchestrator, ['settings'])).toMatch(/^policy open_platform_access on$/m);
    await runnerCaughtUp(orchestrator, 'runner-1');
    const [stopped] = await runnerStore<{ n: number }>(
      orchestrator,
      "SELECT count(*)::int AS n FROM runner.sync_state WHERE key = 'platform_stopped'",
    );
    expect(stopped).toEqual({ n: 0 });

    // 6. Registration stays bound to an invitation, also while every user may invite.
    await setPolicy(orchestrator, OPEN_INVITES, true);
    await anonymousBudget(COST.lookup);
    expect(await anonymous(page, 'POST', '/v1/passkeys/register/begin', {})).toMatchObject({
      status: 400,
      body: { error: 'request_invalid' },
    });
    const prepared = await preparePrivateOrg({
      userId: adaUser.userId,
      identity: adaUser.identity,
      displayName: 'Ada kz59-name-h1p5',
      locale: 'en',
      platformInviteId: '',
    });
    const body: Json = { ...prepared.body };
    delete body.platform_invite_id;
    await expect(adaUser.request('POST', '/v1/orgs', body)).rejects.toThrow(/403 .*platform_invite_required/);

    // 7. Credit 0 with billing active: the task waits and the operator is asked for credit for that organization.
    await billingActive(orchestrator);
    const set = await billing(orchestrator, ['credit', '--org', onOrg, '--set', '0', '--reason', 'e2e59 used up']);
    expect(set.rc, set.stderr).toBe(0);
    const creditTask = await agentTool(
      orchestrator,
      'founder',
      ['task', 'create', '--wg', onWg, '--text', PROMPT.credit],
      120_000,
    );
    closeLater.push(['founder', creditTask.task_id as string]);
    await expect
      .poll(() => taskState(orchestrator, creditTask.task_id as string), { timeout: 60_000 })
      .toEqual({ state: 'scheduling', state_reason: 'credit_exhausted' });
    await expect
      .poll(() => operatorRequests(orchestrator, opal), { timeout: 60_000 })
      .toEqual([`platform_access_required:${offOrg}`, `credit_exhausted:${onOrg}`]);
    expect(await operator(orchestrator, ['waiting'])).toMatch(
      new RegExp(`^org ${onOrg} wg ${onWg} credit_exhausted tasks 1 since \\S+ platform access yes$`, 'm'),
    );
  } finally {
    for (const [home, task] of closeLater)
      await agentTool(orchestrator, home, ['task', 'close', '--task', task]).catch(() => undefined);
    if (spawned !== null) await removeRunner(orchestrator, spawned).catch(() => undefined);
    // later specs expect team organizations without platform access and only inviters inviting
    await setPolicy(orchestrator, OPEN_ACCESS, false).catch(() => undefined);
    await setPolicy(orchestrator, OPEN_INVITES, false).catch(() => undefined);
  }
  await expectNoPlaintext(orchestrator, CANARIES);
  await closingChecks(orchestrator, true);
});
