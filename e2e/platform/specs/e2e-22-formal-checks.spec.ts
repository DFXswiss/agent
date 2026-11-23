// E2E-22 (07-testing.md section 2.1; 03-api.md sections 8.3, 9; 04-crypto.md section 4.10; ADR-012): formal checks and
// duplicates. One session requests operations that the platform must refuse, deduplicate or retry by deterministic
// rules only: the same comment twice (the second is `duplicate` with the original result, GitHub got one POST), the
// same comment again with a new idempotency key (`duplicate` too: a key never lifts the content rule, KOR-28), an
// idempotency key reused with another body (`rejected` `params_invalid`), an unknown type, a repository outside the
// allow-list, a
// protected branch, a repository the owner's GitHub connection cannot reach, a branch whose ref creation first meets a
// 502 (retried inside the same operation), the push of an older commit after a newer one (`non_fast_forward`), a commit
// with a token-shaped secret and a pull request that is already open at GitHub (`existing:true`, MCP `duplicate`).
// Every outcome has its hub record and audit row. Issues (KOR-25): an issue filed twice and a comment on it repeated are
// published once each (`duplicate` with the original's result); the issues of a repository outside the workgroup's
// repository list are refused with `repo_not_allowed` before any GitHub call, shown on the operation card of the
// Operations tab and in the audit screen. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import type { Page } from '@playwright/test';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  allowRepositories,
  armFaults,
  connectGitHub,
  expectCalls,
  expectGitHubOk,
  githubJournal,
  githubRepo,
  githubRoutes,
  hubOperations,
  openOperationsTab,
  operationAudit,
  repository,
  seedGitHub,
  worldOf,
} from '../fixtures/github.ts';
import { navTo } from '../fixtures/nav.ts';
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

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-22-formal-checks.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz22-octo-d8s3j6';
const HELLO = 'octo-org/hello';
const PROMPT = 'E2E22-CHECKS kz22-prompt-m6c1v9x4: comment, retry, push and open the feature pull request';
const CANARIES = [
  LOGIN,
  'kz22-prompt-m6c1v9x4',
  'kz22-answer-q5w9e3r7',
  'kz22-comment-a',
  'kz22-key-a',
  'kz22-key-b',
  'kz22-file-a',
  'kz22-file-b',
  'kz22Fake',
  'kz22-name-u4p7h2k9',
];
const COMMENTS = 'POST /repos/{owner}/{repo}/issues/{number}/comments';
const CREATE_REF = 'POST /repos/{owner}/{repo}/git/refs';

interface JournalEntry {
  readonly path: string;
  readonly body: { readonly messages?: readonly { role: string; content: unknown }[] } | null;
}

/** The MCP results the session got (decoded JSON texts of `tool_result` blocks), in order. */
async function toolResults(orchestrator: Parameters<typeof hubOperations>[0]): Promise<Record<string, unknown>[]> {
  const journal = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as JournalEntry[];
  const last = journal.filter((entry) => entry.path.startsWith('/v1/messages') && !entry.path.includes('count_tokens'));
  const messages = last.at(-1)?.body?.messages ?? [];
  const out: Record<string, unknown>[] = [];
  for (const message of messages) {
    if (message.role !== 'user' || !Array.isArray(message.content)) continue;
    for (const block of message.content as { type: string; content?: unknown }[]) {
      if (block.type !== 'tool_result') continue;
      const content = block.content;
      const text =
        typeof content === 'string' ? content : (content as { text?: string }[]).map((c) => c.text ?? '').join('');
      // Platform answers are JSON objects; the file tools answer with plain text.
      if (text.startsWith('{')) out.push(JSON.parse(text) as Record<string, unknown>);
    }
  }
  return out;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-22 @R-9.4 @R-10.9 @R-10.10 @R-11.1 @R-11.2 @R-11.3 @R-11.4 @R-11.5 @R-11.6 @R-11.7 @R-14.5 @R-P.5 @needs-vm operations are checked formally, deduplicated and retried by deterministic rules', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-22 is a T1 test (07-testing.md section 2.1)');
  test.setTimeout(720_000);
  await orchestrator.loadScenario('model', scenario);
  const feature = {
    name: 'feature',
    from: 'main',
    commits: [{ message: 'Start the feature.', files: { 'feature.md': 'feature\n' } }],
  };
  await seedGitHub(
    orchestrator,
    worldOf(LOGIN, 'octo-org', [
      repository(HELLO, {
        branches: [...(repository(HELLO).branches as unknown[]), feature],
        pulls: [{ title: 'Feature', head: 'feature', base: 'main', draft: false, user: LOGIN }],
      }),
      repository('octo-org/other'),
      { ...repository('octo-org/hidden'), installed: false },
    ]),
  );
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz22-name-u4p7h2k9');
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [HELLO, 'octo-org/hidden']);
  // The first ref creation answers 502 once; the comments may reach GitHub twice (two distinct bodies) and no more.
  await armFaults(orchestrator, [{ route: CREATE_REF, repo: HELLO, kind: 'error', status: 502, times: 1 }]);
  await expectCalls(orchestrator, [{ route: COMMENTS, repo: HELLO, count: 2 }]);
  const connectCalls = (await githubRoutes(orchestrator)).length;

  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'All checks ran kz22-answer-q5w9e3r7.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);

  // The hub records: outcomes with their exact codes, duplicates pointing at the original.
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state, op.error_code])).toEqual([
    ['github.comment', 'succeeded', null],
    ['github.comment', 'duplicate', null],
    ['github.comment', 'duplicate', null],
    ['github.comment', 'succeeded', null],
    ['github.comment', 'rejected', 'params_invalid'],
    ['shell.exec', 'rejected', 'unknown_type'],
    ['github.create_branch', 'rejected', 'repo_not_allowed'],
    ['github.create_branch', 'rejected', 'branch_protected'],
    ['github.checkout', 'rejected', 'repo_not_accessible'],
    ['github.checkout', 'succeeded', null],
    ['github.create_branch', 'succeeded', null],
    ['github.commit', 'succeeded', null],
    ['github.commit', 'succeeded', null],
    ['github.push', 'succeeded', null],
    ['github.push', 'failed', 'non_fast_forward'],
    ['github.commit', 'rejected', 'secret_detected'],
    ['github.create_pull_request', 'succeeded', null],
  ]);
  expect(operations.every((op) => op.session_id === session!.id && op.params_event === 'operation.params')).toBe(true);
  expect([operations[1]!.duplicate_of, operations[2]!.duplicate_of]).toEqual([operations[0]!.id, operations[0]!.id]);
  const finals: Record<string, string> = {
    succeeded: 'operation.succeeded',
    duplicate: 'operation.duplicate',
    rejected: 'operation.rejected',
    failed: 'operation.failed',
  };
  for (const op of operations) expect(await operationAudit(orchestrator, op.id)).toContain(finals[op.state]);

  // What the session got: duplicates carry the original result; the existing pull request is reported as duplicate.
  const results = await toolResults(orchestrator);
  expect(
    results.map((result) => [result.status, (result.error as { code?: string } | undefined)?.code ?? null]),
  ).toEqual([
    ['succeeded', null],
    ['duplicate', null],
    ['duplicate', null],
    ['succeeded', null],
    ['rejected', 'params_invalid'],
    ['rejected', 'unknown_type'],
    ['rejected', 'repo_not_allowed'],
    ['rejected', 'branch_protected'],
    ['rejected', 'repo_not_accessible'],
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['failed', 'non_fast_forward'],
    ['rejected', 'secret_detected'],
    ['duplicate', null],
  ]);
  expect(results[1]!.result).toEqual(results[0]!.result);
  expect(results[2]!.result).toEqual(results[0]!.result);
  expect((results[4]!.error as { pointers?: string[] }).pointers).toEqual(['/idempotency_key']);
  expect(results[16]!.result).toMatchObject({ number: 1, existing: true });

  // GitHub: one POST per distinct comment, the 502 retried inside the branch operation, nothing pushed twice.
  const routes = await githubRoutes(orchestrator, connectCalls);
  expect(routes.filter((route) => route === COMMENTS)).toHaveLength(2);
  expect(routes.filter((route) => route === CREATE_REF)).toHaveLength(2);
  expect(operations.filter((op) => op.type === 'github.create_branch' && op.state === 'succeeded')).toHaveLength(1);
  const repo = await githubRepo(orchestrator, HELLO);
  expect(repo.comments.map((comment) => comment.body)).toEqual(['Looks good kz22-comment-a', 'First keyed kz22-key-a']);
  expect(repo.pulls.map((pull) => pull.number)).toEqual([1]);
  // Refused and failed operations changed nothing at GitHub: exactly the two commits were uploaded (the secret never
  // was), one fast-forward update moved the branch to the newer commit, the older push did not move it back.
  for (const route of [
    'POST /repos/{owner}/{repo}/git/blobs',
    'POST /repos/{owner}/{repo}/git/trees',
    'POST /repos/{owner}/{repo}/git/commits',
  ])
    expect(
      routes.filter((called) => called === route),
      route,
    ).toHaveLength(2);
  expect(routes.filter((route) => route === 'PATCH /repos/{owner}/{repo}/git/refs/{ref}')).toHaveLength(1);
  const pushed = (results[13]!.result as { sha: string }).sha;
  expect(pushed).toBe((results[12]!.result as { commit_sha: string }).commit_sha);
  expect(repo.refs['heads/e2e22/work'] ?? repo.refs['refs/heads/e2e22/work']).toBe(pushed);
  const calls = await githubJournal(orchestrator);
  expect(JSON.stringify(calls)).not.toContain('kz22Fake');

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});

// -- KOR-25: issues written once, a repository outside the list ------------------------------------------------------

const issuesScenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-22-issues.json', import.meta.url), 'utf8'),
);
const CREATE_ISSUE = 'POST /repos/{owner}/{repo}/issues';
const SECRET = 'octo-org/secret';

/** The audit screen filtered to `action` of `task` (the filter form; client-side navigation keeps the unlock). */
async function auditOf(page: Page, task: string, action: string) {
  await navTo(page, 'audit');
  await page.getByTestId('audit.filter.reset').click();
  await page.getByTestId('audit.filter.action').fill(action);
  await page.getByTestId('audit.filter.task').fill(task);
  await page.getByTestId('audit.filter.apply').click();
  await expect(page.getByTestId('audit.loading')).toHaveCount(0, { timeout: 30_000 });
  return page.locator(`[data-testid="audit.row"][data-action="${action}"]`);
}

test('@E2E-22 @R-9.4 @R-11.1 @R-11.4 @R-11.5 @R-11.7 @R-P.5 @needs-vm issues and their comments are published once and a repository outside the list is refused', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-22 is a T1 test (07-testing.md section 2.1)');
  test.setTimeout(600_000);
  const LOGIN = 'kz22-octo-issues-r8m2';
  const TITLE = 'Track the notes kz22-issue-title-h3m8';
  const COMMENT = 'On it kz22-issue-comment-f2t9';
  await orchestrator.loadScenario('model', issuesScenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, 'octo-org', [repository(HELLO), repository(SECRET)]));
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz22-name-issues-d3k6');
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [HELLO]);
  // one POST each: a second issue or comment is a failure of the double
  await expectCalls(orchestrator, [
    { route: CREATE_ISSUE, repo: HELLO, count: 1 },
    { route: COMMENTS, repo: HELLO, count: 1 },
  ]);
  const connectCalls = (await githubRoutes(orchestrator)).length;

  await home(page);
  const task = await sendPrompt(page, 'E2E22-ISSUES kz22-prompt-issues-c7v2: file the notes issue and comment on it');
  await expectAnswerInChat(page, 'Issue #1 is filed and commented kz22-issues-answer-n5c3.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);

  // The hub records: each repeat a duplicate of its original, the foreign repository refused.
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state, op.error_code])).toEqual([
    ['github.create_issue', 'succeeded', null],
    ['github.create_issue', 'duplicate', null],
    ['github.comment', 'succeeded', null],
    ['github.comment', 'duplicate', null],
    ['github.list_issues', 'rejected', 'repo_not_allowed'],
  ]);
  expect(operations.every((op) => op.session_id === session!.id && op.params_event === 'operation.params')).toBe(true);
  expect([operations[1]!.duplicate_of, operations[3]!.duplicate_of]).toEqual([operations[0]!.id, operations[2]!.id]);
  const finals = ['operation.succeeded', 'operation.duplicate', 'operation.succeeded', 'operation.duplicate'];
  for (const [index, action] of finals.entries())
    expect(await operationAudit(orchestrator, operations[index]!.id)).toContain(action);
  expect(await operationAudit(orchestrator, operations[4]!.id)).toContain('operation.rejected');

  // GitHub: one issue, one comment; nothing about the foreign repository was asked.
  const routes = await githubRoutes(orchestrator, connectCalls);
  expect(routes.filter((route) => route === CREATE_ISSUE)).toHaveLength(1);
  expect(routes.filter((route) => route === COMMENTS)).toHaveLength(1);
  expect(routes.filter((route) => route.startsWith('GET /repos/{owner}/{repo}/issues'))).toEqual([]);
  const calls = (await githubJournal(orchestrator)).slice(connectCalls);
  expect(calls.filter((call) => call.path.includes(`/${SECRET}`))).toEqual([]);
  const repo = await githubRepo(orchestrator, HELLO);
  expect(repo.issues.map((issue) => [issue.number, issue.title, issue.labels])).toEqual([[1, TITLE, ['notes']]]);
  expect(repo.comments.map((comment) => [comment.number, comment.body])).toEqual([[1, COMMENT]]);

  // The Operations tab: the duplicates point at their originals, the refused list names its code.
  const cards = await openOperationsTab(page);
  await expect(cards).toHaveCount(5, { timeout: 30_000 });
  await expect(page.locator('[data-testid="operation.card"][data-state="duplicate"]')).toHaveCount(2);
  for (const card of await page.locator('[data-testid="operation.card"][data-state="duplicate"]').all())
    await expect(card.getByTestId('operation.duplicate-of')).toBeVisible();
  const refused = page.locator('[data-testid="operation.card"][data-type="github.list_issues"]');
  await expect(refused).toHaveAttribute('data-state', 'rejected');
  await expect(refused.getByTestId('operation.error')).toHaveAttribute('data-code', 'repo_not_allowed');

  // The audit screen shows the refusal with its code.
  const rows = await auditOf(page, task, 'operation.rejected');
  await expect(rows).toHaveCount(1, { timeout: 30_000 });
  await expect(rows.getByTestId('audit.row.detail')).toContainText('error_code=repo_not_allowed');

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, [
    LOGIN,
    'kz22-prompt-issues-c7v2',
    'kz22-issue-title-h3m8',
    'kz22-issue-body-w6q1',
    'kz22-issue-comment-f2t9',
    'kz22-issues-answer-n5c3',
    'kz22-name-issues-d3k6',
  ]);
});
