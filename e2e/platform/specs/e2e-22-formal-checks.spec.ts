// E2E-22 (07-testing.md section 2.1; 03-api.md sections 8.3, 9; 04-crypto.md section 4.10; ADR-012): formal checks and
// duplicates. One session requests operations that the platform must refuse, deduplicate or retry by deterministic
// rules only: the same comment twice (the second is `duplicate` with the original result, GitHub got one POST), an
// idempotency key reused with another body (`duplicate`), an unknown type, a repository outside the allow-list, a
// protected branch, a repository the owner's GitHub connection cannot reach, a branch whose ref creation first meets a
// 502 (retried inside the same operation), the push of an older commit after a newer one (`non_fast_forward`), a commit
// with a token-shaped secret and a pull request that is already open at GitHub (`existing:true`, MCP `duplicate`).
// Every outcome has its hub record and audit row. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
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
  operationAudit,
  repository,
  seedGitHub,
  worldOf,
} from '../fixtures/github.ts';
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
  // The first ref creation answers 502 once; the comments may reach GitHub twice (one plain, one keyed) and no more.
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
    ['github.comment', 'succeeded', null],
    ['github.comment', 'duplicate', null],
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
  expect([operations[1]!.duplicate_of, operations[3]!.duplicate_of]).toEqual([operations[0]!.id, operations[2]!.id]);
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
    ['succeeded', null],
    ['duplicate', null],
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
  expect(results[3]!.result).toEqual(results[2]!.result);
  expect(results[15]!.result).toMatchObject({ number: 1, existing: true });

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
  const pushed = (results[12]!.result as { sha: string }).sha;
  expect(pushed).toBe((results[11]!.result as { commit_sha: string }).commit_sha);
  expect(repo.refs['heads/e2e22/work'] ?? repo.refs['refs/heads/e2e22/work']).toBe(pushed);
  const calls = await githubJournal(orchestrator);
  expect(JSON.stringify(calls)).not.toContain('kz22Fake');

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});
