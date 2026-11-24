// E2E-21 (07-testing.md section 2.1; 03-api.md sections 8.3, 9; 05-runner.md section 12; ADR-014, ADR-015): the GitHub
// operation chain of a session. With GitHub connected and the repository on the workgroup's allow-list, the session
// lists the repositories, checks one out, creates a branch, writes a file, commits it, pushes, opens a draft pull
// request, comments on it and reads it back. The runner executes every operation on the host against the GitHub
// double, which receives exactly the expected REST sequence; the session gets the pull request's number and URL, the
// chat shows them on the operation card; the hub holds one record per operation with its encrypted params and result
// events and its audit rows, and no pull-request table. Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  allowRepositories,
  connectGitHub,
  expectGitHubOk,
  GITHUB_SITE,
  githubRepo,
  githubRoutes,
  hubOperations,
  operationAudit,
  operationStates,
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
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-21-github-chain.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz21-octo-w3n7c5';
const REPO = 'octo-org/hello';
const PR_URL = `${GITHUB_SITE}/${REPO}/pull/1`;
const PROMPT = 'E2E21-CHAIN kz21-prompt-x4b8m2s6: add notes to octo-org/hello and open a draft pull request';
const CANARIES = [
  LOGIN,
  'kz21-prompt-x4b8m2s6',
  'kz21-answer-z1f5k9m3',
  'kz21-file-g3w8n5c1',
  'kz21-title-p9d4r7v2',
  'kz21-comment-t6j2h8q4',
  'kz21-name-l8v2r6e1',
];
const TYPES = [
  'github.list_repositories',
  'github.checkout',
  'github.create_branch',
  'github.commit',
  'github.push',
  'github.create_pull_request',
  'github.comment',
  'github.get_pull_request',
];
/** The REST calls of the chain at the double (pre-checks and executors of 05-runner.md section 12.1), in order. */
const REST_SEQUENCE = [
  // list_repositories
  'GET /user/installations',
  'GET /user/installations/{installation_id}/repositories',
  // checkout: pre-check (access, default branch, ref), executor (tarball, codeload)
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/commits/{ref}',
  'GET /repos/{owner}/{repo}/tarball/{ref}',
  'GET /codeload/{owner}/{repo}/legacy.tar.gz/{ref}',
  // create_branch
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'POST /repos/{owner}/{repo}/git/refs',
  // commit: pre-check (access, parent, parent tree), executor (blob, tree, commit)
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'GET /repos/{owner}/{repo}/git/commits/{sha}',
  'GET /repos/{owner}/{repo}/git/trees/{tree}',
  'POST /repos/{owner}/{repo}/git/blobs',
  'POST /repos/{owner}/{repo}/git/trees',
  'POST /repos/{owner}/{repo}/git/commits',
  // push: pre-check (access), executor (head, compare, fast-forward update)
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'GET /repos/{owner}/{repo}/compare/{basehead}',
  'PATCH /repos/{owner}/{repo}/git/refs/{ref}',
  // create_pull_request: pre-check (access), executor (open pull requests of head/base, create)
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/pulls',
  'POST /repos/{owner}/{repo}/pulls',
  // comment
  'GET /repos/{owner}/{repo}',
  'POST /repos/{owner}/{repo}/issues/{number}/comments',
  // get_pull_request
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/pulls/{number}',
  'GET /repos/{owner}/{repo}/pulls/{number}/files',
  'GET /repos/{owner}/{repo}/issues/{number}/comments',
];

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

test('@E2E-21 @R-6.3 @R-9.2 @R-9.3 @R-9.5 @R-10.11 @R-11.1 @R-11.8 @R-11.9 @R-14.7 @R-P.5 @needs-vm the session runs the GitHub operation chain through the platform and gets the pull request back', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, 'octo-org', [repository(REPO)]));
  const device = await platformDevice(orchestrator);
  const since = await auditHead(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz21-name-l8v2r6e1');
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [REPO]);
  const connectCalls = (await githubRoutes(orchestrator)).length;

  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, `Draft pull request #1 is open: ${PR_URL} kz21-answer-z1f5k9m3.`);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);

  // The double received exactly the chain's REST calls; the repository holds branch, commit, draft PR and comment.
  expect(await githubRoutes(orchestrator, connectCalls)).toEqual(REST_SEQUENCE);
  const repo = await githubRepo(orchestrator, REPO);
  const pulls = repo.pulls.map((pull) => [pull.number, pull.state, pull.draft, pull.head, pull.base]);
  expect(pulls).toEqual([[1, 'open', true, 'e2e21/notes', 'main']]);
  expect(repo.comments.map((comment) => comment.body)).toEqual(['Ready for review kz21-comment-t6j2h8q4']);

  // One hub record per operation, in order, each with its params and result event, transitions and audit rows.
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state, op.session_id, op.params_event, op.result_event])).toEqual(
    TYPES.map((type) => [type, 'succeeded', session!.id, 'operation.params', 'operation.result']),
  );
  for (const op of operations) {
    expect(await operationStates(orchestrator, op.id)).toEqual(['received', 'accepted', 'executing', 'succeeded']);
    // the lifecycle audits in order; a read of the operation by the browser (`read.operation`, read log) may come
    // between them, whenever the open task shows the operation
    const audit = await operationAudit(orchestrator, op.id);
    expect(
      audit.filter((action) => !action.startsWith('operation.')).every((action) => action === 'read.operation'),
    ).toBe(true);
    expect(audit.filter((action) => action.startsWith('operation.'))).toEqual([
      'operation.accepted',
      'operation.executing',
      'operation.succeeded',
    ]);
  }
  const devices = await orchestrator.hubQuery<{ device_id: string }>(
    `SELECT DISTINCT device_id FROM hub.operation WHERE task_id = '${uuid(task)}'`,
  );
  expect(devices).toEqual([{ device_id: device }]);

  // The results as the user decrypts them: the pull request's number and URL, the pushed commit.
  const results = await taskBodies(page, orchestrator, onboarded, task, 'operation.result');
  const byId = new Map(results.map((body) => [body.operation_id, body]));
  const pr = byId.get(operations[5]!.id) as { status: string; result: { number: number; url: string; draft: boolean } };
  expect(pr.status).toBe('succeeded');
  expect(pr.result).toMatchObject({ number: 1, url: PR_URL, draft: true });
  const commit = byId.get(operations[3]!.id) as { result: { commit_sha: string } };
  const push = byId.get(operations[4]!.id) as { result: { sha: string } };
  expect(push.result.sha).toBe(commit.result.commit_sha);
  expect(repo.refs['heads/e2e21/notes'] ?? repo.refs['refs/heads/e2e21/notes']).toBe(commit.result.commit_sha);

  // The chat shows the operations as cards; the pull-request card links the PR.
  const prCard = page
    .getByTestId('operation.card')
    .filter({ hasText: 'e2e21/notes' })
    .filter({
      has: page.locator('[data-testid="operation.fact"][data-label="pull_request"]'),
    });
  await expect(prCard.first()).toBeVisible({ timeout: 30_000 });
  const link = prCard.first().locator('[data-testid="operation.fact"][data-label="pull_request"] a');
  await expect(link).toHaveText('#1');
  await expect(link).toHaveAttribute('href', PR_URL);

  // No relational pull-request data at the hub (ADR-014).
  const tables = await orchestrator.hubQuery<{ table_name: string }>(
    `SELECT table_name FROM information_schema.tables
      WHERE table_schema = 'hub' AND (table_name LIKE '%pull%' OR table_name ~ '(^|_)prs?(_|$)')`,
  );
  expect(tables).toEqual([]);

  // No inbound refusal of the device during the scenario (KOR-17).
  expect(await inboundRejections(orchestrator, device, since)).toEqual([]);

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});
