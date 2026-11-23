// LIVE-02 (07-testing.md sections 2.4 and 6; LVT-01): E2E-21 against a private test repository at the real GitHub.
// The operator configures the repository (`AGENT_LIVE_GITHUB_REPO`, `owner/name`, a private repository of the test
// account with a `main` branch, only used by this test) and hands over the token of the test account's existing
// GitHub login (`E2E_LIVE_GITHUB_TOKEN`; `live/driver.py`, e.g. from `gh auth token`). No GitHub App exists, so the
// device flow cannot run against GitHub (it stays covered hermetically by E2E-20): the token is deposited directly as
// the user's `github` credential, sealed in Node (the page never holds it). The runners reach GitHub through the live
// bridge (`POST /live/github`: API `<bridge>/live/github`, tarball redirects to codeload through the bridge too);
// the session is scripted by the model double (this test is about GitHub, not the model).
//
// Chain (the session's operations): list repositories (a user token of an OAuth login is refused by GitHub's
// installations endpoint - `upstream_error`, recorded; with a GitHub App user token it lists, E2E-21), checkout,
// create a branch that already exists at GitHub (created by the spec beforehand: the real `422 Reference already
// exists` must become `branch_exists`, B-13), create the run's branch, write a file, commit, push, open a draft pull
// request, comment on it, read it back. Asserted: the REST sequence at the bridge, every operation's hub record,
// states and audit rows, the results the user decrypts, and the pull request, comment and branch head at GitHub.
// Cleanup at the start (leftovers of earlier runs) and at the end, also after a failure: the run's pull requests are
// closed and its branches deleted. Results: `<iteration>/live/live-02.json` (no secret).
import { randomBytes } from 'node:crypto';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import { expectNoCredentialLeak } from '../fixtures/model.ts';
import { allowRepositories, hubOperations, operationAudit, operationStates } from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';
import { bridgeJournal, depositCredential, GitHub, required, writeResult } from './helpers.ts';

const PREFIX = 'live02/';
const TYPES = [
  'github.list_repositories',
  'github.checkout',
  'github.create_branch',
  'github.create_branch',
  'github.commit',
  'github.push',
  'github.create_pull_request',
  'github.comment',
  'github.get_pull_request',
];
const OUTCOMES = [
  ['failed', 'upstream_error'],
  ['succeeded', null],
  ['failed', 'branch_exists'],
  ['succeeded', null],
  ['succeeded', null],
  ['succeeded', null],
  ['succeeded', null],
  ['succeeded', null],
  ['succeeded', null],
];

const operation = (type: string, params: Record<string, unknown>) => ({
  type: 'tool_use',
  name: 'mcp__platform__request_operation',
  input: { type, params },
});

/** The model double's script of the chain (E2E-21's scenario with the live repository, branches and PR number). */
function chainScenario(repo: string, branch: string, existing: string, number: number, marker: string) {
  const url = `https://github.com/${repo}/pull/${number}`;
  const step = (id: string, expect: Record<string, unknown>, content: unknown[], extra: object = {}) => ({
    id,
    expect,
    reply: { content },
    ...extra,
  });
  return {
    name: 'live-02-github',
    description: 'LIVE-02: the E2E-21 operation chain of a session against the real GitHub test repository.',
    side: [{ id: 'title', expect: { has_tools: false }, reply: { content: [{ type: 'text', text: 'Live notes' }] } }],
    steps: [
      step('warm-up', { user_text_contains: 'LIVE02-WARM', has_tool_result: false }, [
        { type: 'text', text: `Ready ${marker}-warm.` },
      ]),
      step('list', { user_text_contains: 'LIVE02-CHAIN', has_tool_result: false, tools_include: ['Write'] }, [
        { type: 'text', text: 'Looking at the repositories first.' },
        operation('github.list_repositories', {}),
      ]),
      step('checkout', { tool_result_contains: 'upstream_error' }, [operation('github.checkout', { repo })]),
      step('branch-exists', { tool_result_contains: '"dir":"repos/' }, [
        operation('github.create_branch', { repo, branch: existing, from: 'main' }),
      ]),
      step('branch', { tool_result_contains: 'branch_exists' }, [
        operation('github.create_branch', { repo, branch, from: 'main' }),
      ]),
      // The checkout directory is `repos/<owner>/<name>` in lower case (the runner's normalised repository name).
      step('write', { tool_result_contains: `"branch":"${branch}"` }, [
        {
          type: 'tool_use',
          name: 'Write',
          input: { file_path: `\${cwd}/repos/${repo.toLowerCase()}/LIVE02.md`, content: `Live notes ${marker}-file\n` },
        },
      ]),
      step('commit', { tool_result_is_error: false }, [
        operation('github.commit', { repo, branch, message: `Add the live notes ${marker}.` }),
      ]),
      step(
        'push',
        { tool_result_contains: '"files_changed":1' },
        [operation('github.push', { repo, branch, commit_sha: '${commit}' })],
        {
          capture: { commit: { regex: '"commit_sha":"([0-9a-f]{40})","files_changed"', source: 'messages' } },
        },
      ),
      step('pull-request', { tool_result_contains: `/tree/${branch}"` }, [
        operation('github.create_pull_request', {
          repo,
          head: branch,
          base: 'main',
          title: `Live notes ${marker}`,
          body: 'Written by the LIVE-02 session; closed by the test.',
          draft: true,
        }),
      ]),
      step('comment', { tool_result_contains: `/pull/${number}"` }, [
        operation('github.comment', { repo, number, body: `Ready for review ${marker}-comment` }),
      ]),
      step('get-pull-request', { tool_result_contains: 'issuecomment-' }, [
        operation('github.get_pull_request', { repo, number }),
      ]),
      step('done', { tool_result_contains: `${marker}-comment` }, [
        { type: 'text', text: `Draft pull request #${number} is open: ${url} ${marker}-done.` },
      ]),
    ],
  };
}

/** The chain's REST calls at the bridge (method, path pattern, status), E2E-21's sequence at the real GitHub. */
function expectedSequence(repo: string, branch: string, number: number) {
  const r = repo.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
  const b = (name: string) => name.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
  const sha = '[0-9a-f]{40}';
  return [
    ['GET', '^/user/installations$', 403],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/commits/[^/]+$`, 200],
    ['GET', `^/repos/${r}/tarball/${sha}$`, 302],
    ['GET', `^/${r}/legacy\\.tar\\.gz/.+$`, 200],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/git/ref/heads/main$`, 200],
    ['POST', `^/repos/${r}/git/refs$`, 422],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/git/ref/heads/main$`, 200],
    ['POST', `^/repos/${r}/git/refs$`, 201],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/git/ref/heads/${b(branch)}$`, 200],
    ['GET', `^/repos/${r}/git/commits/${sha}$`, 200],
    ['GET', `^/repos/${r}/git/trees/${sha}$`, 200],
    ['POST', `^/repos/${r}/git/blobs$`, 201],
    ['POST', `^/repos/${r}/git/trees$`, 201],
    ['POST', `^/repos/${r}/git/commits$`, 201],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/git/ref/heads/${b(branch)}$`, 200],
    ['GET', `^/repos/${r}/compare/.+$`, 200],
    ['PATCH', `^/repos/${r}/git/refs/heads/${b(branch)}$`, 200],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/pulls$`, 200],
    ['POST', `^/repos/${r}/pulls$`, 201],
    ['GET', `^/repos/${r}$`, 200],
    ['POST', `^/repos/${r}/issues/${number}/comments$`, 201],
    ['GET', `^/repos/${r}$`, 200],
    ['GET', `^/repos/${r}/pulls/${number}$`, 200],
    ['GET', `^/repos/${r}/pulls/${number}/files$`, 200],
    ['GET', `^/repos/${r}/issues/${number}/comments$`, 200],
  ].map(([method, pattern, status]) => ({ method, pattern: new RegExp(pattern as string, 'i'), status }));
}

test.describe.configure({ mode: 'serial' });

test('@LIVE-02 @R-9.2 @R-9.3 @R-9.6 @R-11.8 @R-P.6 @live @needs-vm the GitHub operation chain against the private test repository', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(1_200_000);
  const token = required('E2E_LIVE_GITHUB_TOKEN');
  const repo = required('AGENT_LIVE_GITHUB_REPO');
  const run = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
  const branch = `${PREFIX}${run}`;
  const existing = `${PREFIX}${run}-exists`;
  const marker = `LIVE02-${run}`;
  // A live test before this one may have pointed the model upstreams at a real provider: back to the double.
  await orchestrator.call('POST', '/live/model-double', {});
  const { api_url: api } = await orchestrator.call<{ api_url: string }>('POST', '/live/github', {});
  const github = new GitHub(api, token, repo);
  const results: Record<string, unknown> = { test: 'LIVE-02', branch, existing };
  await github.checkPrivate();
  results.leftovers_removed = await github.cleanup(PREFIX);
  try {
    const main = await github.headOf('main');
    expect(main, 'the test repository has a main branch').toMatch(/^[0-9a-f]{40}$/);
    const number = (await github.lastNumber()) + 1;
    results.pull_request = number;
    await github.createBranch(existing, main!);
    await orchestrator.loadScenario('model', chainScenario(repo, branch, existing, number, marker));

    const device = await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, 'Ada Live02');
    // A first task assigns the platform device to the new org, so the deposited credential reaches it at once.
    await sendPrompt(page, 'LIVE02-WARM: say ready.');
    await expectAnswerInChat(page, `Ready ${marker}-warm.`);

    const account = await github.user();
    const owner = await depositCredential(page, onboarded.recoveryCode, 'github', {
      token,
      login: account.login,
      user_id: account.id,
      scopes: account.scopes,
      created_at: new Date().toISOString(),
    });
    await expect
      .poll(
        async () =>
          (
            await runnerStore<{ kind: string }>(
              orchestrator,
              `SELECT kind FROM runner.credential_cache WHERE owner_id = '${uuid(owner)}' AND kind = 'github'`,
            )
          ).length,
        { timeout: 120_000 },
      )
      .toBe(1);
    const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
      `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
    );
    await allowRepositories(page, onboarded.org, org!.default_wg, [repo]);

    await home(page);
    const from = (await bridgeJournal(orchestrator)).length;
    const task = await sendPrompt(
      page,
      `LIVE02-CHAIN ${marker}: add live notes to ${repo} and open a draft pull request`,
    );
    const url = `https://github.com/${repo}/pull/${number}`;
    await expectAnswerInChat(page, `Draft pull request #${number} is open: ${url} ${marker}-done.`);
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

    // The REST sequence at the real GitHub (through the bridge), E2E-21's with the refused list and the real 422.
    const calls = (await bridgeJournal(orchestrator))
      .slice(from)
      .filter((call) => call.upstream === 'github' || call.upstream === 'codeload');
    results.rest = calls.map((call) => `${call.method} ${call.status} ${call.upstream}`);
    const expected = expectedSequence(repo, branch, number);
    expect(calls.length, JSON.stringify(results.rest)).toBe(expected.length);
    expected.forEach((want, index) => {
      const got = calls[index]!;
      expect([got.method, want.pattern.test(got.path), got.status], `call ${index + 1}`).toEqual([
        want.method,
        true,
        want.status,
      ]);
    });

    // One hub record per operation with its outcome, transitions and audit rows.
    const operations = await hubOperations(orchestrator, task);
    expect(operations.map((op) => op.type)).toEqual(TYPES);
    expect(operations.map((op) => [op.state, op.error_code])).toEqual(OUTCOMES);
    for (const op of operations) {
      expect([op.params_event, op.result_event]).toEqual(['operation.params', 'operation.result']);
      const last = op.state === 'succeeded' ? 'operation.succeeded' : 'operation.failed';
      expect((await operationAudit(orchestrator, op.id)).at(-1)).toBe(last);
      expect((await operationStates(orchestrator, op.id)).at(-1)).toBe(op.state);
    }
    const devices = await orchestrator.hubQuery<{ device_id: string }>(
      `SELECT DISTINCT device_id FROM hub.operation WHERE task_id = '${uuid(task)}'`,
    );
    expect(devices).toEqual([{ device_id: device }]);

    // What the user decrypts, and what GitHub holds.
    const bodies = await taskBodies(page, orchestrator, onboarded, task, 'operation.result');
    const byId = new Map(bodies.map((body) => [body.operation_id, body]));
    const pr = byId.get(operations[6]!.id) as { result: { number: number; url: string; draft: boolean } };
    expect(pr.result).toMatchObject({ number, draft: true });
    expect(pr.result.url.toLowerCase()).toBe(url.toLowerCase());
    const commit = (byId.get(operations[4]!.id) as { result: { commit_sha: string } }).result.commit_sha;
    expect(await github.headOf(branch)).toBe(commit);
    const pull = await github.ok<{ state: string; draft: boolean; head: { ref: string } }>(
      'GET',
      `/repos/${repo}/pulls/${number}`,
    );
    expect([pull.state, pull.draft, pull.head.ref]).toEqual(['open', true, branch]);
    const comments = await github.ok<{ body: string }[]>('GET', `/repos/${repo}/issues/${number}/comments`);
    expect(comments.map((comment) => comment.body)).toEqual([`Ready for review ${marker}-comment`]);
    await expectNoPlaintext(orchestrator, [marker]);
    // The token never in plaintext on the runner device, in a VM or at the hub.
    await expectNoCredentialLeak(orchestrator, [token]);
    results.passed = true;
  } finally {
    const cleaned = await github.cleanup(PREFIX);
    results.cleanup = cleaned;
    results.cleanup_complete =
      cleaned.errors.length === 0 &&
      (await github.headOf(branch)) === null &&
      (await github.headOf(existing)) === null &&
      (await github.ok<unknown[]>('GET', `/repos/${repo}/pulls?state=open&per_page=100`)).length === 0;
    writeResult(e2e.iterationDir, 'live-02', results);
    expect(results.cleanup_complete, 'test repository cleaned up').toBe(true);
  }
});
