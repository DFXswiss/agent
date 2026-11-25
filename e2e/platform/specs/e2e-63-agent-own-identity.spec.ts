// E2E-63 (ADR-082, 11-agent-access.md section 2; KOR-57 stage a): the main orchestrator with its own identity and a
// real revocation. Ada pairs her agent with her passkey: the agent holds its own keys only and is her sub-principal
// in her log (`agent_add` by Ada with her certificate). It writes like Ada - a workgroup, a task with a prompt, a
// whiteboard entry - and Ada's web shows "Agent of Ada" as author of each. Attacks while it is active: the agent
// cannot confirm a pairing (`POST /v1/me/agents` is `403 forbidden`) nor add an agent for a foreign key
// (`agent_add` signed by the agent: `log_agent` at the hub and in the reader); a sub-principal without Ada's
// certificate is refused by the hub's append (`log_agent`) and a reader refuses a log that contains one
// (`log_agent`). The agent stores an API key of Ada's under the credential key it rotated; a copy of its state is kept,
// then Ada disconnects the agent in the settings (her browser rotates the credential key and re-encrypts the API key,
// which still opens; she pairs a new agent right after it, which succeeds at once): the log shows
// `remove` of the agent and a rotation of every scope it read; the hub has no wrap to the agent made after the
// revocation and none of a new epoch, so the copy's keys open the agent's own entry but not the one Ada writes
// afterwards, and the copy's requests are answered `agent_revoked`. An old agent - the registering agent of an account
// of an agent alone (ADR-071), which holds the account's identity - converts itself without anyone doing anything
// once a person adds a passkey to the account (web recovery with the account's recovery code, ADR-082 section 9.10):
// its next command is the conversion (`agent_add` by the user in the log, the hub's row `own_identity` without a
// sealed identity, audit `agent.identity`, no identity left in the tool's state). Exactly one (step 1): Ada's second
// pairing replaces her first agent in the same confirmation; no state of the log and no moment at the hub has two
// active agents.
// Stage b (ADR-082 sections 6 and 9.6-9.9, 05-runner.md section 17, 11-agent-access.md section 11; the second test):
// Ada's private org has the platform device; on her running main session she chooses "Make this session my main
// orchestrator" and confirms with her passkey. The page names the agent it replaces (her local agent, revoked at once:
// exactly one); the runner drew the keys in the VM (the tool and its state under the account `agent-orch`, the key
// unreadable for the session user; the hub row holds the public keys only) and the session claims. Run again with the
// second MCP server, the session creates a task and invites a member through the orchestrator's tools - agent
// requests audited for the agent, shown as "Agent of Ada" - and cannot ask to become the orchestrator itself or pair
// (`forbidden`, `not_in_session`); a local agent cannot ask for it or confirm the session's open pairing either. A
// local `pair` replaces the session's orchestrator (exactly one); a further session made orchestrator is revoked
// (`session_ended`) when its task is closed. Both tests run on T1.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  hex,
  navigate,
  newBrowser,
  openScopeEvents,
  personOf,
  refusal,
  visit,
} from '../fixtures/identity.ts';
import { expectNoPlaintext, osRead, sessionState, sessionVm, vmFiles } from '../fixtures/guest.ts';
import { agentState, agentTool, agentToolRun, copyAgentHome, pairAgent } from '../fixtures/agent-tool.ts';
import {
  expectAnswerInChat,
  home,
  invite,
  mainSessions,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { testApiKey } from '../fixtures/model.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { signCert } from '../../../web/src/core/crypto/agent';
import { Identity } from '../../../web/src/core/crypto/keys';
import type { Secret } from '../../../web/src/core/crypto/secret';
import { unwrapKey } from '../../../web/src/core/crypto/wrap';
import { b64uDecode, b64uEncode } from '../../../web/src/core/encoding/b64u';
import { uuid7 } from '../../../web/src/core/encoding/ids';
import { buildEntries, type Entry, refusalCode } from '../../../web/src/core/log/entries';
import { AGENT, epochKey, ORG, WG } from '../../../web/src/core/log/state';
import { verifyLog, type VerifiedLog } from '../../../web/src/core/log/verify';

const ADA = 'Ada Own-Agent';
const BY_AGENT = `Agent of ${ADA}`;
const WG_NAME = 'Own key work kz57-wg-t4r6e8';
const PROMPT = 'E2E58-PROMPT kz57-prompt-b3n5m7: plan the release';
const OLD_ENTRY = 'Written by the agent kz57-old-q2w4e6';
const NEW_ENTRY = 'Written after the disconnect kz57-new-z9x7c5';
const LEGACY = 'Legacy Bot kz57-bot-p1o3i5';
const CANARIES = ['kz57-wg-t4r6e8', 'kz57-prompt-b3n5m7', 'kz57-old-q2w4e6', 'kz57-new-z9x7c5'];
// The computers of this scenario (agent-tool homes) carry their own names: one Playwright run executes several agent
// scenarios against the same orchestrator, whose computers outlive a spec.
const HOME = 'own_ada';
const FIRST = 'own_first';
const NEXT = 'own_next';
const COPY = 'own_copy';
const OLD = 'own_legacy';
/** A generation above every generation the hub has issued in this run. */
const FORGED_GEN = 100_000;

type Json = Record<string, unknown>;

/** The private keys of the agent state on computer `home` (what a copy of that state holds). */
async function heldKeys(
  orchestrator: Orchestrator,
  home: string,
): Promise<{ id: string; keys: Identity; state: Json }> {
  const { state, key } = await agentState(orchestrator, home);
  expect(key, `agent.key on ${home}`).not.toBeNull();
  const raw = b64uDecode(key!, 64);
  return { id: state.agent_id as string, keys: await Identity.fromRaw(raw.slice(0, 32), raw.slice(32)), state };
}

/** An `agent_add` of the agent `subject` with keys `pub` for `user`, its certificate signed by `certifier`. */
async function agentAdd(subject: string, pub: Identity, user: string, certifier: Identity) {
  const fields = { agent: subject, pub: pub.public(), user, gen: FORGED_GEN };
  return {
    type: 'agent_add' as const,
    subject,
    user,
    pub: pub.public(),
    gen: FORGED_GEN,
    cert: await signCert(certifier.sig.privateKey, fields),
  };
}

/** The entry after the head of `log`, written and signed by `by` with `key`. */
async function entryAfter(log: VerifiedLog, org: string, by: string, key: Identity, fields: Json): Promise<Entry> {
  const [entry] = await buildEntries({ org, head: log.head }, { id: by, key: key.sig.privateKey }, [
    fields as Parameters<typeof buildEntries>[2][number],
  ]);
  return entry!;
}

/** The code with which a reader that replays the whole log refuses it once `entry` follows the head. */
async function readerRefusal(user: PageUser, log: VerifiedLog, entry: Entry): Promise<string> {
  try {
    await verifyLog([...log.entries, entry], user.identity.public().sig, await user.platform());
    return 'accepted';
  } catch (error) {
    return refusalCode(error);
  }
}

const append = (log: VerifiedLog, entry: Entry) => ({
  expected_seq: log.head.seq + 1,
  entries: [entry],
  wraps: [],
  events: [],
});

/** The wraps the hub keeps for `agent` (`late`: made after the agent's revocation). */
const agentWraps = (orchestrator: Orchestrator, agent: string) =>
  orchestrator.hubQuery<{ scope: string; scope_id: string; epoch: number; wrap: string; late: boolean }>(
    `SELECT w.scope, w.scope_id::text AS scope_id, w.epoch, encode(w.wrap, 'hex') AS wrap,
            coalesce(w.created_at > a.revoked_at, false) AS late
       FROM hub.key_wrap w JOIN hub.agent a ON a.id = w.recipient_id WHERE w.recipient_id = '${uuid(agent)}'`,
  );

/** The hub's rows of `user`'s agents that are active. */
const activeAgents = async (orchestrator: Orchestrator, user: string) =>
  (
    await orchestrator.hubQuery<{ id: string }>(
      `SELECT id::text AS id FROM hub.agent WHERE user_id = '${uuid(user)}' AND state = 'active'`,
    )
  ).map((row) => row.id);

/** At most one active agent of `user` in every state of the log. */
const neverTwo = (log: VerifiedLog, user: string) =>
  log.states.every((state) => state.agentsOf(user).filter((agent) => state.member(agent.id) !== null).length <= 1);

test('@E2E-63 the main orchestrator acts with its own identity, its revocation is real, an old agent converts itself and nobody inserts an agent', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);

  // 1. Ada pairs her agent with her passkey (twice: the second replaces the first): own keys, a sub-principal of Ada
  // in her log, no identity handed over.
  const ada = await personOf(page, await authenticator('platform'), orchestrator, ADA);
  const adaId = ada.user.userId;
  const org = ada.onboarded.org;
  expect(await pairAgent(orchestrator, page, e2e.hubOrigin, FIRST)).toMatchObject({ ok: true });
  const first = (await agentTool(orchestrator, FIRST, ['status'])).agent_id as string;
  expect(await activeAgents(orchestrator, adaId)).toEqual([first]);
  // Exactly one: the next pairing replaces the active agent in the same confirmation; never two active.
  await visit(page, '/settings');
  const beforeSecond = await ada.user.log(org);
  expect(await pairAgent(orchestrator, page, e2e.hubOrigin, HOME, true)).toMatchObject({ ok: true });
  const status = await agentTool(orchestrator, HOME, ['status']);
  expect(status).toMatchObject({ user_id: adaId });
  const agentId = status.agent_id as string;
  const held = await heldKeys(orchestrator, HOME);
  expect(held.id).toBe(agentId);
  expect(Object.keys(held.state)).not.toContain('bundle_ct');
  expect(Object.keys(held.state)).toContain('user_ct');
  expect(
    await orchestrator.hubQuery<Json>(
      `SELECT own_identity, held_identity, bundle_ct IS NULL AS no_bundle, cert IS NOT NULL AS cert
         FROM hub.agent WHERE id = '${uuid(agentId)}'`,
    ),
  ).toEqual([{ own_identity: true, held_identity: false, no_bundle: true, cert: true }]);
  const paired = await ada.user.log(org);
  const added = paired.entries.find((entry) => entry.body.type === 'agent_add' && entry.body.subject === agentId);
  expect(added?.body).toMatchObject({ by: adaId, user: adaId, pub: held.keys.public() });
  expect(paired.state.activeAgent(adaId)).toMatchObject({ id: agentId, kind: AGENT, parent: adaId });
  expect(await activeAgents(orchestrator, adaId)).toEqual([agentId]);
  expect(
    await orchestrator.hubQuery<Json>(`SELECT state, revoked_reason FROM hub.agent WHERE id = '${uuid(first)}'`),
  ).toEqual([{ state: 'revoked', revoked_reason: 'replaced' }]);
  // the new agent's `agent_add` (a higher generation) removes the first one at the same `seq` (ADR-082 section 9.1)
  const replacing = paired.entries
    .slice(beforeSecond.entries.length)
    .find((entry) => entry.body.type === 'agent_add' && entry.body.subject === agentId)!;
  expect(paired.state.principals.get(first)?.removedSeq).toBe(replacing.body.seq);
  expect(neverTwo(paired, adaId)).toBe(true);
  expect((await agentToolRun(orchestrator, FIRST, ['status'])).result).toMatchObject({
    ok: false,
    error: 'agent_revoked',
  });

  // 2. The agent writes like Ada: a workgroup (a log entry of its own), a task with a prompt, a whiteboard entry.
  const wg = (await agentTool(orchestrator, HOME, ['wg', 'create', '--org', org, '--name', WG_NAME])).wg_id as string;
  const task = await agentTool(orchestrator, HOME, ['task', 'create', '--wg', wg, '--text', PROMPT], 120_000);
  expect(task).toMatchObject({ org_id: org, wg_id: wg });
  const taskId = task.task_id as string;
  expect(
    await agentTool(orchestrator, HOME, ['whiteboard', 'add', '--org', org, '--wg', wg, '--text', OLD_ENTRY]),
  ).toMatchObject({ level: 'workgroup', org_id: org, wg_id: wg });
  const written = await ada.user.log(org);
  expect(written.entries.find((entry) => entry.body.type === 'wg_create')?.body).toMatchObject({ by: agentId });
  expect(
    await orchestrator.hubQuery<{ author_kind: string }>(
      `SELECT DISTINCT author_kind FROM hub.event WHERE author_id = '${uuid(agentId)}'`,
    ),
  ).toEqual([{ author_kind: 'agent' }]);

  // Ada sees all of it in the web, the agent named as "Agent of Ada".
  await visit(page, `/o/${org}`);
  await navigate(page, `/o/${org}/w/${wg}`);
  const row = page.locator(`[data-testid="tasks.row"][data-task="${uuid(taskId)}"]`);
  await expect(row.getByTestId('tasks.row.owner')).toHaveText(BY_AGENT, { timeout: 30_000 });
  await navigate(page, `/o/${org}/t/${taskId}`);
  await expect(page.getByTestId('chat.prompt').filter({ hasText: 'kz57-prompt-b3n5m7' })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId('chat.prompt.author')).toHaveText(BY_AGENT);
  await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
  const oldItem = page.getByTestId('whiteboard.item').filter({ hasText: OLD_ENTRY });
  await expect(oldItem.getByTestId('whiteboard.item.author')).toHaveText(BY_AGENT, { timeout: 30_000 });

  // 3. Attack: the agent pairs an agent. It cannot confirm a pairing ...
  const stranger = await Identity.generate();
  const strangerId = uuid7();
  const confirm = await agentToolRun(orchestrator, HOME, [
    'api',
    'POST',
    '/v1/me/agents',
    '--body',
    JSON.stringify({
      pairing_id: strangerId,
      label_ct: 'A'.repeat(64),
      claim_hash: 'A'.repeat(43),
      gen: FORGED_GEN,
      cert: (await agentAdd(strangerId, stranger, adaId, held.keys)).cert,
      user_ct: 'A'.repeat(64),
      batches: [],
    }),
  ]);
  expect(confirm.rc).toBe(1);
  expect(confirm.result).toMatchObject({ ok: false, error: 'hub_refused', hub_error: 'forbidden', status: 403 });
  // ... nor add an agent for a foreign key to the log: only Ada's identity signs certificates.
  const before = await ada.user.log(org);
  const byAgent = await entryAfter(
    before,
    org,
    agentId,
    held.keys,
    await agentAdd(strangerId, stranger, adaId, held.keys),
  );
  const pushed = await agentToolRun(orchestrator, HOME, [
    'api',
    'POST',
    `/v1/orgs/${org}/log`,
    '--body',
    JSON.stringify(append(before, byAgent)),
  ]);
  expect(pushed.rc).toBe(1);
  expect(pushed.result).toMatchObject({ ok: false, error: 'hub_refused', hub_error: 'log_agent' });
  expect(await readerRefusal(ada.user, before, byAgent)).toBe('log_agent');

  // 4. Attack: the hub inserts a sub-principal of Ada without her signature. It holds no key of Ada's, so its agent
  // either adds itself with a certificate of another key, or the entry names Ada with that certificate: the hub's own
  // append refuses both (only the author appends its entries; the certificate must verify under Ada's key) and a
  // reader that replays the log refuses it (`log_agent`).
  const hubAgent = await Identity.generate();
  const hubAgentId = uuid7();
  const forged = await agentAdd(hubAgentId, hubAgent, adaId, hubAgent);
  const selfAdded = await entryAfter(before, org, hubAgentId, hubAgent, forged);
  const namedAda = await entryAfter(before, org, adaId, ada.user.identity, forged);
  expect(await refusal(ada.user, 'POST', `/v1/orgs/${org}/log`, append(before, selfAdded))).toBe('403 forbidden');
  expect(await refusal(ada.user, 'POST', `/v1/orgs/${org}/log`, append(before, namedAda))).toMatch(/^4\d\d log_agent$/);
  expect(await readerRefusal(ada.user, before, selfAdded)).toBe('log_agent');
  expect(await readerRefusal(ada.user, before, namedAda)).toBe('log_agent');
  expect((await ada.user.log(org)).head).toEqual(before.head);

  // 5. The agent stores an API key of Ada's under the credential key it rotated after replacing the first agent.
  await agentTool(orchestrator, HOME, ['credentials', 'set-api-key'], 60_000, testApiKey());
  const credential = async () =>
    (
      await orchestrator.hubQuery<{ epoch: number; header_text: string }>(
        `SELECT epoch, header_text FROM hub.credential WHERE owner_id = '${uuid(adaId)}' AND kind = 'anthropic_api_key'`,
      )
    )[0]!;
  const storedKey = await credential();
  expect(JSON.parse(storedKey.header_text)).toMatchObject({ author: agentId });
  expect(
    await orchestrator.hubQuery<Json>(
      `SELECT wrapped_by::text AS by FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = '${uuid(adaId)}'
         AND recipient_id = '${uuid(adaId)}' AND epoch = ${storedKey.epoch}`,
    ),
  ).toEqual([{ by: agentId }]);
  // A copy of the agent's state, then Ada disconnects the agent in the settings.
  await copyAgentHome(orchestrator, HOME, COPY);
  const copy = await heldKeys(orchestrator, COPY);
  const heldWraps = await agentWraps(orchestrator, agentId);
  expect(heldWraps.filter((wrap) => wrap.scope === WG && wrap.scope_id === wg).length).toBeGreaterThan(0);
  const scopes: [string, string][] = [
    [ORG, org],
    ...[...before.state.workgroups.keys()].map((id) => [WG, id] as [string, string]),
  ];
  const epochsBefore = scopes.map(([scope, id]) => before.state.epoch(scope, id));
  await visit(page, '/settings');
  const item = page.locator(`[data-testid="agents.item"][data-agent="${agentId}"]`);
  await expect(item.getByTestId('agents.item.runs')).toBeVisible({ timeout: 30_000 });
  await expect(item.getByTestId('agents.item.held')).toHaveCount(0);
  await item.getByTestId('agents.item.disconnect').click();
  await expect(page.getByTestId('agents.dialog.body')).toBeVisible();
  await expect(page.getByTestId('agents.dialog.held')).toHaveCount(0); // it never held a copy of Ada's identity
  await page.getByTestId('agents.dialog.confirm').click();
  await expect(page.getByTestId('agents.dialog')).toHaveCount(0, { timeout: 120_000 });
  await expect(item).toHaveAttribute('data-state', 'revoked');
  expect(
    await orchestrator.hubQuery<Json>(`SELECT state, revoked_reason FROM hub.agent WHERE id = '${uuid(agentId)}'`),
  ).toEqual([{ state: 'revoked', revoked_reason: 'revoked' }]);

  // The log: the agent removed, every scope it read rotated in the same batch.
  const after = await ada.user.log(org);
  const batch = after.entries.slice(before.entries.length).map((entry) => entry.body);
  expect(batch[0]).toMatchObject({ type: 'remove', subject: agentId, by: adaId });
  expect(batch.slice(1).every((body) => body.type === 'rotate')).toBe(true);
  expect(after.state.member(agentId)).toBeNull();
  expect(after.state.activeAgent(adaId)).toBeNull();
  expect(scopes.map(([scope, id]) => after.state.epoch(scope, id))).toEqual(epochsBefore.map((epoch) => epoch + 1));
  // Her browser rotated the credential key the disconnect owed: the API key the agent stored under its epoch was
  // opened and is re-encrypted by Ada under a new epoch, and it still opens in her settings.
  await expect
    .poll(async () => (await credential()).epoch, { timeout: 60_000, intervals: [500] })
    .toBeGreaterThan(storedKey.epoch);
  expect(JSON.parse((await credential()).header_text)).toMatchObject({ author: adaId });
  await visit(page, '/settings');
  await expect(page.getByTestId('credentials.item.anthropic_api_key')).toHaveAttribute('data-status', 'verified', {
    timeout: 30_000,
  });

  // Right after the disconnect Ada pairs a new agent: the credential-key rotation the disconnect owed ran once (no
  // second rotation at the next sign-in races the confirmation), and the confirmation succeeds at once.
  expect(await pairAgent(orchestrator, page, e2e.hubOrigin, NEXT)).toMatchObject({ ok: true });
  const next = (await agentTool(orchestrator, NEXT, ['status'])).agent_id as string;
  expect(await activeAgents(orchestrator, adaId)).toEqual([next]);
  const withNext = await ada.user.log(org);
  expect(withNext.state.activeAgent(adaId)).toMatchObject({ id: next, parent: adaId });
  expect(withNext.state.member(agentId)).toBeNull();

  // 6. Ada writes a new entry. No wrap to the agent was made after its revocation, none of a new epoch.
  await visit(page, `/o/${org}`);
  await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(NEW_ENTRY);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: NEW_ENTRY })).toBeVisible({ timeout: 30_000 });
  // The hub deleted the agent's org and workgroup wraps with its removal (02-data-model.md section 2.2); what is left
  // (its credential key wraps) is older than the revocation, and nothing of a new epoch was wrapped to it.
  const left = await agentWraps(orchestrator, agentId);
  expect(left.filter((wrap) => wrap.late)).toEqual([]);
  expect(left.filter((wrap) => wrap.scope === ORG || wrap.scope === WG)).toEqual([]);
  for (const [index, [scope, id]] of scopes.entries()) {
    const fresh = [...heldWraps, ...left].filter(
      (wrap) => wrap.scope === scope && wrap.scope_id === id && wrap.epoch > epochsBefore[index]!,
    );
    expect(fresh, `${scope} ${id}`).toEqual([]);
  }
  // The copy's keys open every wrap it was given before (as a copy of the hub's data would hold them) and with them
  // the agent's own entry, never Ada's new one.
  const copyKeys = new Map<string, Secret>();
  for (const wrap of heldWraps.filter((found) => found.scope === ORG || found.scope === WG)) {
    const slot = { scope: wrap.scope, scope_id: wrap.scope_id, epoch: wrap.epoch, recipient: copy.id };
    copyKeys.set(
      epochKey(wrap.scope, wrap.scope_id, wrap.epoch),
      await unwrapKey(copy.keys.kem, slot, b64uEncode(hex(wrap.wrap))),
    );
  }
  const copyView = await openScopeEvents(orchestrator, ada.user, { kind: WG, id: wg }, copyKeys, ['whiteboard.entry']);
  expect(copyView.opened.join('\n')).toContain('kz57-old-q2w4e6');
  expect(copyView.opened.join('\n')).not.toContain('kz57-new-z9x7c5');
  expect(copyView.locked).toEqual([after.state.epoch(WG, wg)]);
  // and the hub refuses the copy
  expect(
    (await agentToolRun(orchestrator, COPY, ['whiteboard', 'list', '--org', org, '--wg', wg])).result,
  ).toMatchObject({
    ok: false,
    error: 'agent_revoked',
  });

  // 7. An old agent converts itself without anyone doing anything. The registering agent of an account of an agent
  // alone holds the account's identity (the form of every agent before ADR-082); a person then adds a passkey to the
  // account through the web recovery with the account's recovery code.
  const invitation = await invite(orchestrator, 'user');
  await anonymousBudget(3 * COST.lookup);
  const made = await agentTool(
    orchestrator,
    OLD,
    ['register', '--own-account', '--display-name', LEGACY, '--name', 'Claude Code', '--host', 'legacy'],
    120_000,
    `${e2e.hubOrigin}/invite#${invitation.code}`,
  );
  const legacyUser = made.user_id as string;
  const legacyAgent = made.agent_id as string;
  const legacyOrg = made.org_id as string;
  const legacyRow = () =>
    orchestrator.hubQuery<Json>(
      `SELECT own_identity, held_identity, bundle_ct IS NULL AS no_bundle, cert IS NOT NULL AS cert
         FROM hub.agent WHERE id = '${uuid(legacyAgent)}'`,
    );
  expect(await legacyRow()).toEqual([{ own_identity: false, held_identity: true, no_bundle: false, cert: false }]);
  expect(Object.keys((await agentState(orchestrator, OLD)).state)).toContain('bundle_ct');
  const person = await newBrowser(browser, e2e);
  await anonymousBudget(COST.page + COST.recover);
  await person.page.goto('/login');
  await person.page.getByTestId('login.recover').click();
  await expect(person.page).toHaveURL(/\/recover$/);
  await person.page.getByTestId('recover.code').fill(made.recovery_code as string);
  await person.page.getByTestId('recover.submit').click();
  await person.page.getByTestId('recover.create-passkey').click();
  await person.page.getByTestId('recover.unlock').click();
  const code = (await person.page.getByTestId('recovery-code.code').textContent())!.trim();
  await person.page.getByTestId('recovery-code.continue').click();
  await expect(person.page).toHaveURL(/\/o\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  expect(await person.authenticator.credentials()).toHaveLength(1);
  // nothing changed until the agent's next command
  expect(await legacyRow()).toEqual([{ own_identity: false, held_identity: true, no_bundle: false, cert: false }]);
  const listed = await agentTool(orchestrator, OLD, ['org', 'list']);
  expect(JSON.stringify(listed)).toContain(legacyOrg);
  expect(await legacyRow()).toEqual([{ own_identity: true, held_identity: true, no_bundle: true, cert: true }]);
  const legacyState = Object.keys((await agentState(orchestrator, OLD)).state);
  expect(legacyState).not.toContain('bundle_ct');
  expect(legacyState).toContain('user_ct');
  expect(
    await orchestrator.hubQuery<{ action: string }>(
      `SELECT action FROM hub.audit WHERE action = 'agent.identity' AND target_id = '${uuid(legacyAgent)}'`,
    ),
  ).toEqual([{ action: 'agent.identity' }]);
  const personUser = await PageUser.open(person.page, code);
  const legacyLog = await personUser.log(legacyOrg);
  expect(
    legacyLog.entries.find((entry) => entry.body.type === 'agent_add' && entry.body.subject === legacyAgent)?.body,
  ).toMatchObject({ by: legacyUser, user: legacyUser });
  expect(legacyLog.state.activeAgent(legacyUser)).toMatchObject({ id: legacyAgent, parent: legacyUser });
  // it keeps working as itself; the settings say it once held a copy of the identity
  expect(await agentTool(orchestrator, OLD, ['status'])).toMatchObject({
    user_id: legacyUser,
    agent_id: legacyAgent,
  });
  await visit(person.page, '/settings');
  await expect(
    person.page.locator(`[data-testid="agents.item"][data-agent="${legacyAgent}"]`).getByTestId('agents.item.held'),
  ).toBeVisible({ timeout: 30_000 });
  await person.context.close();

  // Stage b (KOR-57 b, the orchestrator as a platform session) is the next test of this file.

  await expectNoPlaintext(orchestrator, CANARIES);
  await closingChecks(orchestrator);
});

// --- stage b: the main orchestrator as a platform session ---

const SESSION_SCENARIO = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-63-session-orchestrator.json', import.meta.url), 'utf8'),
) as Json;
const ADA_B = 'Ada Session-Agent';
const BY_SESSION_AGENT = `Agent of ${ADA_B}`;
const FIRST_PROMPT = 'E2E63-SESSION kz63-first-a8s2d6: get ready';
const ACT_PROMPT = 'E2E63-ACT kz63-act-f3g7h1: create the task and invite Grace';
const SECOND_PROMPT = 'E2E63-SECOND kz63-second-j5k9l3: get ready again';
const CHILD_TEXT = 'kz63-child-m3n7b1';
const SESSION_CANARIES = ['kz63-first-a8s2d6', 'kz63-act-f3g7h1', 'kz63-second-j5k9l3', CHILD_TEXT];
const LOCAL_FIRST = 'own_session_first';
const LOCAL_NEXT = 'own_session_next';
const ORCH_TOOL = '/opt/agent-orch';
const ORCH_KEY = '/var/lib/agent-orch/state/agent.key';
const SECOND_MCP = '/opt/agent-guest/mcp-orchestrator.json';

/** The hub's row of the orchestrator agent of `session` (newest), with what it holds. */
const sessionAgent = async (orchestrator: Orchestrator, session: string) =>
  (
    await orchestrator.hubQuery<Json>(
      `SELECT id::text AS id, user_id::text AS user_id, state, revoked_reason, device_id::text AS device_id,
              own_identity, held_identity, bundle_ct IS NULL AS no_bundle, user_ct IS NULL AS no_user_ct,
              length(kem_pub) AS kem, length(sig_pub) AS sig, device_sig IS NOT NULL AS device_sig
         FROM hub.agent WHERE session_id = '${uuid(session)}' ORDER BY created_at DESC LIMIT 1`,
    )
  )[0];

interface OrchestratorAnswer {
  readonly args: readonly string[];
  readonly value: Record<string, unknown>;
}

/** The answers of the orchestrator's `run` tool in the newest model request that carries them. */
async function orchestratorAnswers(orchestrator: Orchestrator): Promise<OrchestratorAnswer[]> {
  const journal = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as {
    path: string;
    body: { messages?: { role: string; content: unknown }[] } | null;
  }[];
  let found: OrchestratorAnswer[] = [];
  for (const entry of journal) {
    if (!entry.path.startsWith('/v1/messages') || entry.path.includes('count')) continue;
    const calls = new Map<string, readonly string[]>();
    const answers: OrchestratorAnswer[] = [];
    for (const message of entry.body?.messages ?? []) {
      if (!Array.isArray(message.content)) continue;
      for (const block of message.content as Record<string, unknown>[]) {
        if (block.type === 'tool_use' && block.name === 'mcp__orchestrator__run')
          calls.set(String(block.id), (block.input as { args: string[] }).args);
        const args = block.type === 'tool_result' ? calls.get(String(block.tool_use_id)) : undefined;
        if (args === undefined) continue;
        const content = block.content;
        const text =
          typeof content === 'string' ? content : (content as { text: string }[]).map((c) => c.text).join('');
        answers.push({ args, value: JSON.parse(text) as Record<string, unknown> });
      }
    }
    if (answers.length > 0) found = answers;
  }
  return found;
}

/**
 * On the page of the running main session `session`, Ada makes it her main orchestrator: the dialog names the agent
 * it replaces, waits for the session's keys, and after the passkey gesture for the session's claim. Returns the agent.
 */
async function makeSessionOrchestrator(
  orchestrator: Orchestrator,
  page: Page,
  org: string,
  session: string,
  replaces: string,
): Promise<string> {
  await visit(page, `/o/${org}/s/${session}`);
  await page.getByTestId('orchestrator.make').click({ timeout: 60_000 });
  const dialog = page.getByTestId('orchestrator.dialog');
  await expect(dialog.getByTestId('orchestrator.session')).toHaveText('This session');
  await expect(dialog.getByTestId('orchestrator.access')).toHaveText(
    'This session receives full access to your account and can do everything you can.',
  );
  await expect(dialog.getByTestId('orchestrator.replaces')).toHaveText(
    `Replaces “${replaces}”, your current main orchestrator. “${replaces}” is disconnected at once.`,
    { timeout: 30_000 },
  );
  // the runner drew the keys in the VM and registered them; the page checked the device's signature over them
  await expect(dialog.getByTestId('orchestrator.confirm')).toBeEnabled({ timeout: 90_000 });
  await expect(dialog.getByTestId('orchestrator.error')).toHaveCount(0);
  const open = await sessionAgent(orchestrator, session);
  expect(open).toMatchObject({ state: 'open', kem: 32, sig: 32, device_sig: true, no_bundle: true });
  await anonymousBudget(COST.signIn);
  await dialog.getByTestId('orchestrator.confirm').click();
  await expect(dialog.getByTestId('orchestrator.outcome.connected')).toHaveText(
    'This session is now your main orchestrator.',
    { timeout: 180_000 },
  );
  await dialog.getByTestId('orchestrator.close').click();
  await expect(page.getByTestId('orchestrator.current')).toBeVisible({ timeout: 30_000 });
  return open!.id as string;
}

/** Waits until the session's agent runs again with the orchestrator's MCP server (after the claim, once idle). */
async function withSecondMcp(orchestrator: Orchestrator, session: string): Promise<void> {
  await expect
    .poll(async () => (await sessionVm(orchestrator, session)).command.join(' '), {
      timeout: 120_000,
      intervals: [1000],
    })
    .toContain(SECOND_MCP);
}

test('@E2E-63 @needs-vm stage b: a running main session becomes the main orchestrator with the passkey gesture, acts through the platform tools and ends with the session', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'the session orchestrator runs on T1');
  test.setTimeout(1_200_000);
  await platformDevice(orchestrator);
  await poolReady(orchestrator, 2);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, ADA_B);
  const adaId = ada.user.userId;
  const org = ada.onboarded.org;
  const [{ default_wg: wg }] = (await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg::text AS default_wg FROM hub.org WHERE id = '${uuid(org)}'`,
  )) as [{ default_wg: string }];
  await orchestrator.call('POST', '/doubles/model/scenario', {
    scenario: SESSION_SCENARIO,
    variables: { org, wg },
    access: null,
  });
  // Her local agent is the main orchestrator first.
  expect(await pairAgent(orchestrator, page, e2e.hubOrigin, LOCAL_FIRST)).toMatchObject({ ok: true });
  const local = (await agentTool(orchestrator, LOCAL_FIRST, ['status'])).agent_id as string;

  // 1. Ada's task runs on the platform device; the session is not offered to anyone but her and has no orchestrator
  // tools yet. The golden image carries the agent tool for the account `agent-orch` only.
  await visit(page, `/o/${org}`);
  const task = await sendPrompt(page, FIRST_PROMPT);
  await expectAnswerInChat(page, 'Session ready kz63-ready-h4j8k2.');
  const [main] = await mainSessions(orchestrator, task);
  const session = main!.id;
  await expect.poll(() => sessionState(orchestrator, session), { timeout: 60_000 }).toBe('idle');
  const tool = Object.values(await vmFiles(orchestrator, [ORCH_TOOL, `${ORCH_TOOL}/bin/agent-platform`], session))[0]!;
  expect(tool[ORCH_TOOL]).toMatchObject({ owner: 'root:agent-orch', mode: '0o750' });
  expect(tool[`${ORCH_TOOL}/bin/agent-platform`]).not.toBeNull();
  // Attack: an agent cannot ask for it (only the task owner's browser session can).
  const asked = await agentToolRun(orchestrator, LOCAL_FIRST, [
    'api',
    'POST',
    `/v1/sessions/${session}/orchestrator`,
    '--body',
    '{}',
  ]);
  expect(asked.result).toMatchObject({ ok: false, error: 'hub_refused', hub_error: 'forbidden', status: 403 });
  expect(
    await orchestrator.hubQuery<Json>(
      `SELECT orchestrator_at IS NULL AS none FROM hub.session WHERE id = '${uuid(session)}'`,
    ),
  ).toEqual([{ none: true }]);

  // 2. The gesture. The page asks for keys, the runner draws them in the VM and registers them with the device's
  // signature; while the pairing is open, her agent cannot confirm it - only her passkey can.
  await visit(page, `/o/${org}/s/${session}`);
  await page.getByTestId('orchestrator.make').click({ timeout: 60_000 });
  await expect(page.getByTestId('orchestrator.confirm')).toBeEnabled({ timeout: 90_000 });
  const pending = (await sessionAgent(orchestrator, session))!;
  const stranger = await Identity.generate();
  const confirmed = await agentToolRun(orchestrator, LOCAL_FIRST, [
    'api',
    'POST',
    '/v1/me/agents',
    '--body',
    JSON.stringify({
      pairing_id: pending.id,
      label_ct: 'A'.repeat(64),
      claim_hash: 'A'.repeat(43),
      gen: FORGED_GEN,
      cert: (await agentAdd(pending.id as string, stranger, adaId, stranger)).cert,
      user_ct: 'A'.repeat(64),
      batches: [],
    }),
  ]);
  expect(confirmed.result).toMatchObject({ ok: false, error: 'hub_refused', hub_error: 'forbidden', status: 403 });
  await page.getByTestId('orchestrator.close').click();
  const agent = await makeSessionOrchestrator(orchestrator, page, org, session, 'Claude Code');
  expect(agent).toBe(pending.id);

  // The session's agent: active, bound to the session and its device, the hub holds its public keys only; the key
  // stays in the VM, readable for `agent-orch` only. Exactly one: the local agent was revoked at once.
  expect(await sessionAgent(orchestrator, session)).toMatchObject({
    id: agent,
    user_id: adaId,
    state: 'active',
    device_id: main!.device_id,
    own_identity: true,
    held_identity: false,
    no_bundle: true,
    kem: 32,
    sig: 32,
  });
  expect(
    await orchestrator.hubQuery<Json>(`SELECT state, revoked_reason FROM hub.agent WHERE id = '${uuid(local)}'`),
  ).toEqual([{ state: 'revoked', revoked_reason: 'replaced' }]);
  expect(await activeAgents(orchestrator, adaId)).toEqual([agent]);
  const withSession = await ada.user.log(org);
  expect(withSession.state.activeAgent(adaId)).toMatchObject({ id: agent, parent: adaId });
  expect(neverTwo(withSession, adaId)).toBe(true);
  const key = Object.values(await vmFiles(orchestrator, [ORCH_KEY], session))[0]![ORCH_KEY];
  expect(key).toMatchObject({ owner: 'agent-orch:agent-orch', mode: '0o600' });
  const read = await osRead(orchestrator, session, ORCH_KEY);
  expect(read.as_user.ok).toBe(false);
  expect(read.in_sandbox.ok).toBe(false);
  await visit(page, '/settings');
  const item = page.locator(`[data-testid="agents.item"][data-agent="${agent}"]`);
  await expect(item.getByTestId('agents.item.session')).toHaveText(
    'On DFX hardware, in a session of one of your tasks',
    { timeout: 30_000 },
  );

  // 3. The session runs again with the orchestrator's MCP server and acts for Ada: a task and an invitation (agent
  // requests with the hub's checks and audit); it can neither ask to become the orchestrator nor pair.
  await withSecondMcp(orchestrator, session);
  await visit(page, `/o/${org}/t/${task}`);
  await page.getByTestId('chat.composer.prompt').fill(ACT_PROMPT);
  await page.getByTestId('chat.composer.send').click();
  await expectAnswerInChat(page, 'Orchestrated kz63-done-q7w1e5.');
  const answers = await orchestratorAnswers(orchestrator);
  const answer = (word: string) => answers.find((found) => found.args.slice(0, 2).join(' ') === word)?.value;
  expect(answer('task create')).toMatchObject({ org_id: org, wg_id: wg });
  expect(answer('org invite')).toHaveProperty('invite_link');
  expect(answer(`api POST`)).toMatchObject({ ok: false, error: 'hub_refused', hub_error: 'forbidden', status: 403 });
  expect(answers.find((found) => found.args[0] === 'pair')?.value).toMatchObject({
    ok: false,
    error: 'not_in_session',
  });
  const child = answer('task create')!.task_id as string;
  expect(
    (
      await orchestrator.hubQuery<{ action: string }>(
        `SELECT DISTINCT action FROM hub.audit WHERE agent_id = '${uuid(agent)}'
           AND action IN ('task.create', 'invite.create') ORDER BY action`,
      )
    ).map((row) => row.action),
  ).toEqual(['invite.create', 'task.create']);
  await navigate(page, `/o/${org}/w/${wg}`);
  const childRow = page.locator(`[data-testid="tasks.row"][data-task="${uuid(child)}"]`);
  await expect(childRow.getByTestId('tasks.row.owner')).toHaveText(BY_SESSION_AGENT, { timeout: 30_000 });

  // 4. A local `pair` replaces the session's orchestrator: exactly one.
  await visit(page, '/settings');
  expect(await pairAgent(orchestrator, page, e2e.hubOrigin, LOCAL_NEXT, true)).toMatchObject({ ok: true });
  const next = (await agentTool(orchestrator, LOCAL_NEXT, ['status'])).agent_id as string;
  expect(await sessionAgent(orchestrator, session)).toMatchObject({ state: 'revoked', revoked_reason: 'replaced' });
  expect(await activeAgents(orchestrator, adaId)).toEqual([next]);

  // 5. The end of a session ends its orchestrator: Ada closes the task, makes the main session of a new task her
  // orchestrator and closes that task too - its agent is revoked with the session (`session_ended`).
  await visit(page, `/o/${org}/t/${task}`);
  await page.getByTestId('task.close').click();
  await page.getByTestId('task.close.confirm').click();
  await expect.poll(() => sessionState(orchestrator, session), { timeout: 120_000, intervals: [1000] }).toBe('stopped');
  await home(page);
  const second = await sendPrompt(page, SECOND_PROMPT);
  await expectAnswerInChat(page, 'Second session ready kz63-second-p2o6i4.');
  const [secondMain] = await mainSessions(orchestrator, second);
  await expect.poll(() => sessionState(orchestrator, secondMain!.id), { timeout: 60_000 }).toBe('idle');
  const ended = await makeSessionOrchestrator(orchestrator, page, org, secondMain!.id, 'Claude Code');
  expect(await activeAgents(orchestrator, adaId)).toEqual([ended]);
  await visit(page, `/o/${org}/t/${second}`);
  await page.getByTestId('task.close').click();
  await page.getByTestId('task.close.confirm').click();
  await expect
    .poll(async () => (await sessionAgent(orchestrator, secondMain!.id))?.revoked_reason, {
      timeout: 120_000,
      intervals: [1000],
    })
    .toBe('session_ended');
  expect(await activeAgents(orchestrator, adaId)).toEqual([]);
  expect(await sessionState(orchestrator, secondMain!.id)).toBe('stopped');

  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expectNoPlaintext(orchestrator, SESSION_CANARIES);
  await closingChecks(orchestrator, true);
});
