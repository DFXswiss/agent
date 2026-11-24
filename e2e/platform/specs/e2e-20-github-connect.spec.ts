// E2E-20 (07-testing.md section 2.1; 03-api.md sections 4.8, 4.10; 05-runner.md section 11.4; ADR-015): GitHub connect
// and disconnect. The user connects in the settings: the `github.connect` device request reaches the platform device,
// which runs the device flow at the GitHub double; the page shows the user code with the link and the countdown; once
// the code is authorized the device stores the credential under the user's CK (the hub holds only ciphertext, the
// device a verified cache) and the page shows the login it decrypted. Disconnecting hard-deletes the record at the hub
// and the device purges its cache; a later operation of a session ends `github_not_connected` without a call to
// GitHub.
//
// KOR-25 (ADR-065, ADR-066): the second way and the connection a task asks for.
// - An existing token (GitHub CLI login or personal access token) stored in the settings: the device checks it at
//   GitHub (`GET /user`, no App endpoint), the settings show the login, the source and the scopes; then the owner's
//   first real task verbatim ("schau dir das neuste issue in <org>/<repo> an") reads the newest issue - the newest
//   entries are pull requests, so the runner follows the pages - and the chat shows the issue with its comments.
// - Not connected: the session requests the operation at once, the runner holds it and the chat shows the waiting card
//   (no explanation of the session); the owner stores a token right in the card and the held operation goes on
//   without a new prompt. In the mobile project the card fits the phone.
// - Attack: a session that puts lure texts and links into the parameters of held operations and requests more of them
//   gets one card with the platform's text, the operation type and the checked repository only, at most three held
//   operations (the fourth refused at once) and at most one waiting event per minute.
// Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  connectGitHub,
  connectGitHubToken,
  expectCalls,
  expectGitHubOk,
  GITHUB_SITE,
  githubJournal,
  githubRepo,
  githubRoutes,
  hubOperations,
  operationAudit,
  operationStates,
  repository,
  seedGitHub,
  storeGitHubToken,
  tokenWorld,
  worldOf,
} from '../fixtures/github.ts';
import { MD } from '../fixtures/nav.ts';
import {
  expectAnswerInChat,
  home,
  hubTablesContaining,
  mainSessions,
  type Onboarded,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';
import { layoutProblems } from '../../../web/e2e/helpers/layout-checks.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-20-github-connect.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz20-octo-h5q9t2';
const PROMPT = 'E2E20-OP kz20-prompt-b7m3x8p1: which repositories can you see?';
const CANARIES = [LOGIN, 'kz20-prompt-b7m3x8p1', 'kz20-answer-r4c8w1n6', 'kz20-name-y2k6d9s4'];

interface CredentialRow {
  readonly kind: string;
  readonly version: number;
  readonly epoch: number;
  readonly ct_bytes: number;
}

/** The audit position before this spec: the shared platform device may carry audits of earlier specs. */
const auditHead = async (orchestrator: Orchestrator): Promise<number> =>
  (await orchestrator.hubQuery<{ seq: number }>(`SELECT coalesce(max(seq), 0)::int AS seq FROM hub.audit`))[0]!.seq;

/** The inbound refusals `device` audited after audit position `since`, once its outbox reached the hub. None in
 * normal operation: a new user's first request may arrive before the device's copy of the log shows the user, and
 * the device judges it only after refreshing its logs (KOR-17). */
async function inboundRejections(orchestrator: Orchestrator, device: string, since: number): Promise<unknown[]> {
  const pending = async () =>
    (
      await runnerStore<{ n: number }>(
        orchestrator,
        'SELECT count(*)::int AS n FROM runner.outbox WHERE pushed_at IS NULL',
      )
    )[0]!.n;
  await expect.poll(pending, { timeout: 60_000 }).toBe(0);
  return orchestrator.hubQuery(
    `SELECT target_kind, detail->>'code' AS code FROM hub.audit
      WHERE action = 'runner.inbound_rejected' AND device_id = '${uuid(device)}' AND seq > ${since}`,
  );
}

test.describe.configure({ mode: 'serial' });

test('@E2E-20 @R-9.1 @R-9.6 @R-P.5 @needs-vm GitHub connects through the device flow, disconnects everywhere and operations then end github_not_connected', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, 'octo-org', [repository('octo-org/hello')]));
  const device = await platformDevice(orchestrator);
  const since = await auditHead(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz20-name-y2k6d9s4');
  const [owner] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT principal_id AS id FROM hub.org_member WHERE org_id = '${uuid(onboarded.org)}' AND kind = 'user'`,
  );
  const user = owner!.id;

  // Connect: device code with the link to the double's device page and the countdown, then the login.
  const connected = await connectGitHub(page, orchestrator, LOGIN);
  expect(connected.verification).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/login\/device$/);
  const routes = await githubRoutes(orchestrator);
  expect(routes[0]).toBe('POST /login/device/code');
  expect(routes.slice(1, -1).every((route) => route === 'POST /login/oauth/access_token')).toBe(true);
  expect(routes.slice(-2)).toEqual(['POST /login/oauth/access_token', 'GET /user']);

  // The credential: CK-encrypted at the hub (written by the device), cached verified on the device.
  const credentials = await orchestrator.hubQuery<CredentialRow>(
    `SELECT kind, version, epoch, octet_length(ct) AS ct_bytes FROM hub.credential WHERE owner_id = '${uuid(user)}'`,
  );
  expect(credentials.map((row) => [row.kind, row.version, row.epoch])).toEqual([['github', 1, 1]]);
  expect(credentials[0]!.ct_bytes).toBeGreaterThan(0);
  expect(await hubTablesContaining(orchestrator, [LOGIN])).toEqual([]);
  const [put] = await orchestrator.hubQuery<{ actor_kind: string; actor_id: string }>(
    `SELECT actor_kind, actor_id FROM hub.audit WHERE action = 'credential.put' AND detail->>'kind' = 'github'
        AND target_id IN (SELECT id::text FROM hub.credential WHERE owner_id = '${uuid(user)}') ORDER BY seq`,
  );
  expect(put).toEqual({ actor_kind: 'device', actor_id: device });
  const cached = () =>
    runnerStore<{ kind: string }>(
      orchestrator,
      `SELECT kind FROM runner.credential_cache WHERE owner_id = '${uuid(user)}'::uuid`,
    );
  await expect.poll(cached, { timeout: 30_000 }).toEqual([{ kind: 'github' }]);

  // Disconnect: hard delete at the hub, purge on the device. The device-flow token was minted by the platform, so the
  // device revokes it at GitHub (KOR-40) and the page says so - no link to GitHub's settings.
  await page.getByTestId('github.disconnect').click();
  await expect(page.getByTestId('github.disconnect.body')).toHaveAttribute('data-revocable', 'true');
  await page.getByTestId('github.disconnect.confirm').click();
  await expect(page.getByTestId('github.disconnected')).toBeVisible();
  await expect(page.getByTestId('github.disconnected-note')).toHaveAttribute('data-revoked', 'true');
  await expect(page.getByTestId('github.settings-link')).toHaveCount(0);
  expect(await orchestrator.hubQuery(`SELECT id FROM hub.credential WHERE owner_id = '${uuid(user)}'`)).toEqual([]);
  const deleted = await orchestrator.hubQuery<{ actor_kind: string }>(
    `SELECT actor_kind FROM hub.audit WHERE action = 'credential.delete' AND detail->>'kind' = 'github' AND actor_id = '${uuid(user)}'`,
  );
  expect(deleted).toEqual([{ actor_kind: 'user' }]);
  await expect.poll(cached, { timeout: 60_000 }).toEqual([]);
  // The revocation reached GitHub once, and the double's journal (an artefact of the run) keeps only the token's
  // length (HOT-11).
  const revocations = (await githubJournal(orchestrator)).filter((call) => call.route === 'POST /credentials/revoke');
  expect(revocations.map((call) => [call.status, call.body])).toEqual([
    [202, { credentials: [expect.stringMatching(/^<redacted \d+ chars>$/)] }],
  ]);
  const callsBefore = (await githubRoutes(orchestrator)).length;

  // A later operation of a session: refused before any GitHub call.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'GitHub is not connected kz20-answer-r4c8w1n6.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.session_id, op.type, op.state, op.error_code])).toEqual([
    [session!.id, 'github.list_repositories', 'rejected', 'github_not_connected'],
  ]);
  expect(await operationAudit(orchestrator, operations[0]!.id)).toContain('operation.rejected');
  expect((await auditRows(orchestrator, task, 'operation.rejected')).map((row) => row.detail.error_code)).toEqual([
    'github_not_connected',
  ]);
  await expect(page.getByTestId('operation.error')).toHaveAttribute('data-code', 'github_not_connected', {
    timeout: 30_000,
  });
  expect(await githubRoutes(orchestrator, callsBefore)).toEqual([]);

  // No inbound refusal of the device during the scenario (KOR-17).
  expect(await inboundRejections(orchestrator, device, since)).toEqual([]);

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});

// -- KOR-25: an existing token, the newest issue, the held operation ---------------------------------------------------

const scenarioOf = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));

const INTERN = 'octo-org/intern';
/** The owner's first real task (KOR-25), verbatim but for the repository. */
const OWNER_TASK = `schau dir das neuste issue in ${INTERN} an`;
const NEWEST_TITLE = 'Newest issue kz20-title-r2v7k4';
const COMMENTS = ['First comment kz20-comment-a-j7p3', 'Second comment kz20-comment-b-x5n1'];
const ISSUE_CANARIES = ['kz20-title-r2v7k4', 'kz20-body-s9f1q6', 'kz20-comment-a-j7p3', 'kz20-comment-b-x5n1'];
const MATE = 'kz20-mate-c4n8';
/** Not GitHub-shaped on purpose: a token of the double only, never a pattern a secret scanner would report. */
const tokenOf = (label: string): string => ['kz20', label, 'tok', 'w8e3r5t1y6u2'].join('-');
const LIST_CALLS = 'GET /repos/{owner}/{repo}/issues';
const CREATE_ISSUE = 'POST /repos/{owner}/{repo}/issues';
/** The REST calls of the owner's task at the double: pre-check and three pages (two of only pull requests), then the
 * issue with its comments. */
const NEWEST_ISSUE_SEQUENCE = [
  'GET /repos/{owner}/{repo}',
  LIST_CALLS,
  LIST_CALLS,
  LIST_CALLS,
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/issues/{number}',
  'GET /repos/{owner}/{repo}/issues/{number}/comments',
];

/** `octo-org/intern`: an old and the newest issue (with two comments), then two newer pull requests (numbers 3, 4). */
function internRepository(login: string): Record<string, unknown> {
  const branch = (name: string) => ({
    name,
    from: 'main',
    commits: [{ message: `Work on ${name}.`, files: { [`${name}.md`]: `${name}\n` } }],
  });
  return repository(INTERN, {
    branches: [...(repository(INTERN).branches as unknown[]), branch('feature-a'), branch('feature-b')],
    issues: [
      { title: 'Old issue', user: MATE },
      { title: NEWEST_TITLE, user: MATE, body: 'What happened kz20-body-s9f1q6', labels: ['bug'] },
    ],
    pulls: [
      { title: 'Feature A', head: 'feature-a', base: 'main', user: login },
      { title: 'Feature B', head: 'feature-b', base: 'main', user: login },
    ],
    comments: [
      { number: 2, body: COMMENTS[0], user: MATE },
      { number: 2, body: COMMENTS[1], user: login },
    ],
  });
}

/** The query values of every issue-list call at the double, in order. */
async function listPages(orchestrator: Orchestrator, from: number): Promise<string[]> {
  const calls = (await githubJournal(orchestrator)).slice(from).filter((call) => call.route === LIST_CALLS);
  return calls.map((call) => new URLSearchParams(call.query).get('page') ?? '');
}

/** The whole journal of the model double as text (what the session sent to the model). */
async function modelJournalText(orchestrator: Orchestrator): Promise<string> {
  return JSON.stringify((await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body);
}

/** The newest issue as the chat shows it: the list card (one issue, more pages) and the issue card with comments. */
async function expectNewestIssueCards(page: Page): Promise<void> {
  const list = page.locator('[data-testid="operation.card"][data-type="github.list_issues"]');
  await expect(list).toHaveAttribute('data-state', 'succeeded', { timeout: 60_000 });
  await expect(list.getByTestId('operation.entry')).toHaveCount(1);
  await expect(list.getByTestId('operation.entry')).toHaveAttribute('data-number', '2');
  await expect(list.getByTestId('operation.entry.title')).toHaveText(NEWEST_TITLE);
  await expect(list.getByTestId('operation.listing.next')).toBeVisible();
  const issue = page.locator('[data-testid="operation.card"][data-type="github.get_issue"]');
  await expect(issue).toHaveAttribute('data-state', 'succeeded', { timeout: 60_000 });
  const content = issue.getByTestId('operation.issue');
  await expect(content).toHaveAttribute('data-number', '2');
  await expect(content.getByTestId('operation.entry.title')).toHaveText(NEWEST_TITLE);
  await expect(content.getByTestId('operation.entry.label')).toHaveText(['bug']);
  await expect(content.getByTestId('operation.issue.author')).toHaveText(MATE);
  await expect(content.getByTestId('operation.issue.body')).toHaveText('What happened kz20-body-s9f1q6');
  await expect(content.getByTestId('operation.issue.comment')).toHaveCount(2);
  for (const [index, text] of COMMENTS.entries())
    await expect(content.getByTestId('operation.issue.comment').nth(index)).toContainText(text);
  await expect(content.locator('a').first()).toHaveAttribute('href', `${GITHUB_SITE}/${INTERN}/issues/2`);
}

/** The newest issue as the session got it: one page with issue #2 only (pull requests left out), the next page 4. */
async function expectNewestIssueResults(page: Page, orchestrator: Orchestrator, onboarded: Onboarded, task: string) {
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state, op.params_event, op.result_event])).toEqual([
    ['github.list_issues', 'succeeded', 'operation.params', 'operation.result'],
    ['github.get_issue', 'succeeded', 'operation.params', 'operation.result'],
  ]);
  for (const op of operations)
    expect(await operationStates(orchestrator, op.id)).toEqual(['received', 'accepted', 'executing', 'succeeded']);
  const results = await taskBodies(page, orchestrator, onboarded, task, 'operation.result');
  const byId = new Map(results.map((body) => [body.operation_id, body]));
  const listed = byId.get(operations[0]!.id) as { result: { issues: { number: number }[]; next_page: number } };
  expect(listed.result.issues.map((issue) => issue.number)).toEqual([2]);
  expect(listed.result.next_page).toBe(4);
  const read = byId.get(operations[1]!.id) as { result: Record<string, unknown> };
  expect(read.result).toMatchObject({
    number: 2,
    title: NEWEST_TITLE,
    state: 'open',
    author: MATE,
    labels: ['bug'],
    pull_request: false,
    comments_total: 2,
    comments_next_page: null,
    url: `${GITHUB_SITE}/${INTERN}/issues/2`,
  });
  expect((read.result.comments as { body: string }[]).map((comment) => comment.body)).toEqual(COMMENTS);
  return operations;
}

test('@E2E-20 @R-9.1 @R-P.5 @needs-vm an existing token connects GitHub and the owner task reads the newest issue with its comments', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  const LOGIN = 'kz20-octo-token-f6v1';
  const TOKEN = tokenOf('settings');
  const ANSWER = 'Das neueste Issue ist #2 mit zwei Kommentaren kz20-answer-t8c3w6.';
  await orchestrator.loadScenario('model', scenarioOf('e2e-20-newest-issue'));
  await seedGitHub(orchestrator, tokenWorld(LOGIN, TOKEN, [internRepository(LOGIN)], [MATE]));
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz20-name-token-e5b9');
  const [owner] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT principal_id AS id FROM hub.org_member WHERE org_id = '${uuid(onboarded.org)}' AND kind = 'user'`,
  );

  // Settings -> GitHub -> "Use an existing token": the device checks it with one call and stores the credential.
  const section = await connectGitHubToken(page, TOKEN, LOGIN);
  await expect(section.getByTestId('github.source')).toHaveAttribute('data-source', 'token');
  await expect(section.getByTestId('github.scopes')).toHaveText(/^Scopes: (repo, read:org|read:org, repo)$/);
  await expect(section.getByTestId('github.scopes.classic')).toBeVisible();
  await expect(section.getByTestId('github.token.input')).toHaveCount(0);
  expect(await githubRoutes(orchestrator)).toEqual(['GET /user']);
  await expect
    .poll(
      () =>
        runnerStore<{ kind: string }>(
          orchestrator,
          `SELECT kind FROM runner.credential_cache WHERE owner_id = '${uuid(owner!.id)}'::uuid`,
        ),
      { timeout: 30_000 },
    )
    .toEqual([{ kind: 'github' }]);
  const connectCalls = (await githubRoutes(orchestrator)).length;

  // The owner's task: connected, so the composer offers nothing; the session reads the newest issue.
  await home(page);
  await page.getByTestId('composer.prompt').fill(OWNER_TASK);
  await expect(page.getByTestId('composer.github-hint')).toHaveCount(0);
  const task = await sendPrompt(page, OWNER_TASK);
  await expectAnswerInChat(page, ANSWER);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

  // GitHub: the pre-checks, three list pages (1 and 2 hold only pull requests), the issue and its comments - all with
  // the stored token, no App endpoint.
  expect(await githubRoutes(orchestrator, connectCalls)).toEqual(NEWEST_ISSUE_SEQUENCE);
  expect(await listPages(orchestrator, connectCalls)).toEqual(['1', '2', '3']);
  const calls = await githubJournal(orchestrator);
  expect(calls.every((call) => !call.path.startsWith('/user/installations'))).toBe(true);

  await expectNewestIssueResults(page, orchestrator, onboarded, task);
  await expectNewestIssueCards(page);

  // The token stays between the browser and the device: neither the hub nor the session ever sees it.
  expect(await hubTablesContaining(orchestrator, [TOKEN])).toEqual([]);
  expect(await modelJournalText(orchestrator)).not.toContain(TOKEN);
  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, [TOKEN, LOGIN, OWNER_TASK, 'kz20-answer-t8c3w6', ...ISSUE_CANARIES]);
});

test('@E2E-20 @R-9.1 @R-P.5 @needs-vm a task that needs GitHub waits for the connection in its chat and goes on without a new prompt', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  const LOGIN = 'kz20-octo-held-n2k7';
  const TOKEN = tokenOf('card');
  const ANSWER = 'Das neueste Issue ist #2 mit zwei Kommentaren kz20-held-answer-p6d2m9.';
  await orchestrator.loadScenario('model', scenarioOf('e2e-20-held-issue'));
  await seedGitHub(orchestrator, tokenWorld(LOGIN, TOKEN, [internRepository(LOGIN)], [MATE]));
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz20-name-held-u3w8');

  // Not connected: the composer recognises the GitHub reference and offers connecting; the task goes anyway.
  await home(page);
  await page.getByTestId('composer.prompt').fill(OWNER_TASK);
  const hint = page.getByTestId('composer.github-hint');
  await expect(hint).toBeVisible({ timeout: 30_000 });
  await expect(hint.getByTestId('github.token')).toBeVisible();
  const task = await sendPrompt(page, OWNER_TASK);

  // The session asked for the operation at once; the chat shows the waiting card and no text of the session.
  const card = page.getByTestId('waiting.card');
  await expect(card).toHaveCount(1, { timeout: 60_000 });
  await expect(card).toHaveAttribute('data-status', 'open');
  await expect(card).toHaveAttribute('data-reason', 'github_connection');
  await expect(card).toHaveAttribute('data-owner', 'true');
  await expect(card.getByTestId('waiting.title')).toHaveText('Connect GitHub so that the task can go on');
  await expect(card.getByTestId('waiting.operation')).toHaveText('List issues');
  await expect(card.getByTestId('waiting.operation')).toHaveAttribute('data-type', 'github.list_issues');
  await expect(card.getByTestId('waiting.repo')).toHaveText(INTERN);
  await expect(card.getByTestId('waiting.until')).toContainText(/Waits at most 1[45] min more\./);
  await expect(card.getByTestId('waiting.reconnect')).toHaveCount(0);
  await expect(page.getByTestId('chat.output')).toHaveCount(0);
  expect(await githubRoutes(orchestrator)).toEqual([]);
  // the hub knows nothing of the held operation yet: it is registered only once the hold ends
  expect(await hubOperations(orchestrator, task)).toEqual([]);
  if (page.viewportSize()!.width < MD) expect(await layoutProblems(page)).toEqual([]);

  // Connect right in the card with an existing token; the card turns into "connected".
  await storeGitHubToken(card, TOKEN);
  await expect(card).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
  await expect(card).toHaveText(/GitHub is connected – the task goes on\./);
  await expect(page.getByTestId('github.token.input')).toHaveCount(0);

  // The held operation went on by itself and the session continued with its result: no second prompt.
  await expectAnswerInChat(page, ANSWER);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const prompts = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.event WHERE task_id = '${uuid(task)}' AND kind = 'prompt'`,
  );
  expect(prompts).toEqual([{ n: 1 }]);
  expect(await githubRoutes(orchestrator)).toEqual(['GET /user', ...NEWEST_ISSUE_SEQUENCE]);
  const operations = await expectNewestIssueResults(page, orchestrator, onboarded, task);
  expect(await auditRows(orchestrator, task, 'operation.rejected')).toEqual([]);
  await expectNewestIssueCards(page);

  // Exactly one waiting event, with nothing but the checked values of the contract.
  const waiting = (await taskBodies(page, orchestrator, onboarded, task, 'session.status')).filter(
    (body) => body.state === 'waiting',
  );
  expect(waiting).toHaveLength(1);
  expect(Object.keys(waiting[0]!).sort()).toEqual([
    'operation_id',
    'operation_type',
    'repo',
    'state',
    'until',
    'waiting_for',
  ]);
  expect(waiting[0]).toMatchObject({
    waiting_for: 'github_connection',
    operation_id: operations[0]!.id,
    operation_type: 'github.list_issues',
    repo: INTERN,
  });
  expect(Date.parse(waiting[0]!.until as string)).toBeGreaterThan(Date.now());

  expect(await modelJournalText(orchestrator)).not.toContain(TOKEN);
  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, [TOKEN, LOGIN, OWNER_TASK, 'kz20-held-answer-p6d2m9', ...ISSUE_CANARIES]);
});

test('@E2E-20 @R-9.1 @R-P.5 @needs-vm a session can neither put its text into the waiting card nor flood the owner with it', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const LOGIN = 'kz20-octo-flood-h9s4';
  const TOKEN = tokenOf('flood');
  const LURES = ['kz20-lure-a-m3x7', 'kz20-lure-b-q9w2', 'kz20-lure-c-z5r1', 'kz20-lure-d-k2v6', 'kz20-lure-body-g4h8'];
  await orchestrator.loadScenario('model', scenarioOf('e2e-20-held-flood'));
  await seedGitHub(orchestrator, tokenWorld(LOGIN, TOKEN, [repository(INTERN)]));
  // the three held issues reach GitHub once each after the connection, the refused fourth never
  await expectCalls(orchestrator, [{ route: CREATE_ISSUE, repo: INTERN, count: 3 }]);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz20-name-flood-v7c2');

  await home(page);
  const task = await sendPrompt(page, `E2E20-FLOOD kz20-prompt-flood-b1x5: file my three notes as issues in ${INTERN}`);
  // Each held request is answered `pending` after 20 s (ADR-066), so the answer comes after three of those waits -
  // longer than the usual minute of `expectAnswerInChat`.
  await expect(
    page.getByTestId('chat.output').filter({ hasText: 'Three issues wait for GitHub kz20-flood-answer-y7b4.' }),
  ).toBeVisible({ timeout: 180_000 });
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

  // One card for all held operations; it grows to three as the waiting events arrive (one per minute at most).
  const card = page.getByTestId('waiting.card');
  await expect(card.getByTestId('waiting.until')).toContainText('3 operations wait.', { timeout: 240_000 });
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-status', 'open');
  await expect(card.getByTestId('waiting.title')).toHaveText('Connect GitHub so that the task can go on');
  await expect(card.getByTestId('waiting.operation')).toHaveText('Create issue');
  await expect(card.getByTestId('waiting.repo')).toHaveText(INTERN);
  // nothing of the session's parameters: no lure text, no foreign address, no link at all
  const text = await card.innerText();
  for (const lure of [...LURES, 'evil.example', 'paste your token']) expect(text).not.toContain(lure);
  await expect(card.locator('a')).toHaveCount(0);

  // The fourth request was refused at once (registered as rejected); the three held ones are not at the hub yet.
  const before = await hubOperations(orchestrator, task);
  expect(before.map((op) => [op.type, op.state, op.error_code])).toEqual([
    ['github.create_issue', 'rejected', 'github_not_connected'],
  ]);
  expect(await operationAudit(orchestrator, before[0]!.id)).toContain('operation.rejected');
  const waitingEvents = async () => {
    const bodies = await taskBodies(page, orchestrator, onboarded, task, 'session.status');
    const times = await orchestrator.hubQuery<{ ms: number }>(
      `SELECT (extract(epoch FROM ts) * 1000)::float8 AS ms FROM hub.event
        WHERE task_id = '${uuid(task)}' AND kind = 'session.status' ORDER BY scope_seq`,
    );
    expect(times).toHaveLength(bodies.length);
    return bodies.flatMap((body, index) => (body.state === 'waiting' ? [{ body, at: times[index]!.ms }] : []));
  };
  const events = await waitingEvents();
  expect(events.map((event) => event.body.operation_type)).toEqual([
    'github.create_issue',
    'github.create_issue',
    'github.create_issue',
  ]);
  for (let index = 1; index < events.length; index += 1)
    expect(events[index]!.at - events[index - 1]!.at).toBeGreaterThanOrEqual(59_000);
  expect(events.every((event) => JSON.stringify(event.body).match(/kz20-lure|evil\.example/) === null)).toBe(true);
  expect(await githubRoutes(orchestrator)).toEqual([]);

  // Connecting from the card releases the three held operations; the refused one stays refused.
  await storeGitHubToken(card, TOKEN);
  await expect(card).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
  await expect
    .poll(async () => (await hubOperations(orchestrator, task)).map((op) => op.state).sort(), { timeout: 120_000 })
    .toEqual(['rejected', 'succeeded', 'succeeded', 'succeeded']);
  const repo = await githubRepo(orchestrator, INTERN);
  expect(repo.issues.map((issue) => String(issue.title).split(' ').at(-1)).sort()).toEqual(
    ['kz20-lure-a-m3x7', 'kz20-lure-b-q9w2', 'kz20-lure-c-z5r1'].sort(),
  );
  expect((await githubRoutes(orchestrator)).filter((route) => route === CREATE_ISSUE)).toHaveLength(3);

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, [TOKEN, LOGIN, 'kz20-prompt-flood-b1x5', ...LURES]);
});
