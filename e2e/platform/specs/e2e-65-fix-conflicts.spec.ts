// E2E-65 (07-testing.md section 2.1; KOR-48, ADR-084; concept ideas 40, 41, A2): "Fix conflicts" end to end. The
// GitHub double holds a repository with a foreign pull request (#1, seeded, not created by the platform):
// 1. a task creates pull request #2 through the platform operations (checkout, branch, write a file, commit, push,
//    create_pull_request);
// 2. the base branch advances with a change that conflicts with both pull requests (`POST /_double/commits`), and the
//    double keeps the first reads of `mergeable` unknown (fault `mergeable_unknown`, 3 reads = one device request);
// 3. the task's "Pull requests" tab shows #2 "being checked" first, then "conflict";
// 4. "Fix conflicts" sends the fixed platform instruction v1 to the SAME task: the model double's request for that
//    turn holds the whole earlier turn and the conversation keeps its Claude session ID;
// 5. the scripted AI calls `github.merge_base` (content conflict of NOTES.md), tries `github.commit` with `merge: true`
//    while the markers are left (refused `conflict_markers`), reads the file with the diff3 markers, writes it
//    resolved, commits with `merge: true` and pushes;
// 6. in the double the pull request's branch points to a merge commit with two parents (old head, base tip) whose
//    first parent is the old head (a fast-forward, `force: false`), the pull request stays open, nothing merged it;
//    the tab says "Conflicts fixed, branch updated" with that commit and the state turns "no conflict";
// 7. the foreign pull request is linked to the task by pasting its address in the workgroup's "Pull requests" view;
//    the paired agent tool lists both (`pr list --task`), shows the foreign one (`pr show`), finds the existing link
//    (`pr link` answers `created: false`) and sends the instruction for it (`pr fix-conflicts`), which the same session
//    resolves the same way on the foreign branch after checking that branch out (the runner counts a workspace that
//    holds the merge commit of #2 as dirty for #1: merge commit with two parents, pull request open);
// 8. zero knowledge: the hub holds the two links as opaque tags with sealed bodies of fixed length, and none of the
//    repository, branch, login, file, title or prompt texts reaches the hub side (plaintext scan of the database dump,
//    the logs and what crossed the hub's links).
// Runs on T1.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { Locator, Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { agentTool, pairAgent } from '../fixtures/agent-tool.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  armFaults,
  connectGitHub,
  expectGitHubOk,
  githubJournal,
  githubRepo,
  hubOperations,
  repository,
  seedGitHub,
  worldOf,
} from '../fixtures/github.ts';
import { navTo } from '../fixtures/nav.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
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
  readFileSync(new URL('../scenarios/e2e-65-fix-conflicts.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz61-octo-h2v6';
const MATE = 'kz61-mate-j8r3';
const OWNER = 'kz61-org';
const REPO = `${OWNER}/kz61-app-w5q8`;
const HEAD = 'kz61-notes-b7t2';
const FOREIGN = 'kz61-foreign-c3n9';
const OURS = `${REPO}#2`;
const THEIRS = `${REPO}#1`;
const NAME = 'Ada kz61-name-u3e7';
const PROMPT = `E2E61-PR kz61-prompt-d3f7: add notes to ${REPO} and open a pull request`;
const OPENED = 'Pull request #2 is open kz61-answer-m2q6.';
const FIXED = `Conflicts fixed, the branch ${HEAD} is updated kz61-fixed-n6c2.`;
const HOME = 'ada61';
const CANARIES = [
  LOGIN,
  MATE,
  REPO,
  'kz61-app-w5q8',
  HEAD,
  FOREIGN,
  'kz61-name-u3e7',
  'kz61-prompt-d3f7',
  'kz61-answer-m2q6',
  'kz61-fixed-n6c2',
  'kz61-foreign-fixed-t4h9',
  'kz61-head-notes-p4w9',
  'kz61-base-notes-k8m3',
  'kz61-foreign-line-v5d1',
  'kz61-base-line-r2x6',
  'kz61-title-s8k3',
  'kz61-foreign-title-f6w1',
];
const CREATE_COMMIT = 'POST /repos/{owner}/{repo}/git/commits';
const UPDATE_REF = 'PATCH /repos/{owner}/{repo}/git/refs/{ref}';
/** The fixed platform instruction v1 (ADR-084 section 3; web `core/pulls/instruction.ts`, `agent_platform/pulls.py`). */
const instruction = (repo: string, number: number): string =>
  [
    `Platform instruction "fix merge conflicts" (v1) for pull request ${repo}#${number}:`,
    `1. Call github.merge_base with {"repo": "${repo}", "number": ${number}}.`,
    '2. If the result says up_to_date, say so and stop.',
    '3. Resolve every listed conflict in the workspace directory of the repository, keeping the intent of both sides; remove every conflict marker; record a decision for every conflict without markers.',
    '4. If the repository has tests, run them with test.run on the resolved workspace and fix what the merge broke before you commit.',
    "5. Call github.commit with merge: true on the pull request's branch, then github.push of that commit to the same branch.",
    '6. Never merge, close or force-push the pull request.',
    '7. If a resolution needs a decision of a person, ask with human.question and wait for the answer.',
  ].join('\n');

/** The installation's user, a second account (author of the foreign pull request) and the repository: `main` with
 *  README.md and CHANGELOG.md, the foreign branch changing the changelog's top line, and its open pull request #1. */
function world() {
  const base = worldOf(LOGIN, OWNER, [
    repository(REPO, {
      branches: [
        {
          name: 'main',
          commits: [
            {
              message: 'Initial commit.',
              files: { 'README.md': 'hello\n', 'CHANGELOG.md': '# Changelog\n- 0.1 first release\n' },
            },
          ],
        },
        {
          name: FOREIGN,
          from: 'main',
          commits: [
            {
              message: 'Note the foreign change.',
              files: { 'CHANGELOG.md': '# Changelog\n- 0.2 kz61-foreign-line-v5d1\n- 0.1 first release\n' },
            },
          ],
        },
      ],
      pulls: [{ title: 'Foreign change kz61-foreign-title-f6w1', head: FOREIGN, base: 'main', user: MATE }],
    }),
  ]);
  return { ...base, users: [...base.users, { login: MATE, id: 7002, name: 'Octo Mate', email: null }] };
}

interface Person {
  readonly name: string;
  readonly email: string;
  readonly date: string;
}

interface CommitBody {
  readonly message: string;
  readonly tree: string;
  readonly parents: readonly string[];
  readonly author: Person;
  readonly committer: Person;
  readonly signature?: string;
}

/** The git object ID of the commit the double stored from a `POST …/git/commits` body (unsigned; as in E2E-60). */
function commitId(body: CommitBody): string {
  const person = (who: Person) => `${who.name} <${who.email}> ${Date.parse(who.date) / 1000} +0000`;
  const lines = [`tree ${body.tree}`, ...body.parents.map((parent) => `parent ${parent}`)];
  lines.push(`author ${person(body.author)}`, `committer ${person(body.committer)}`);
  const payload = Buffer.from(`${lines.join('\n')}\n\n${body.message}`, 'utf8');
  return createHash('sha1')
    .update(Buffer.concat([Buffer.from(`commit ${payload.length}\0`), payload]))
    .digest('hex');
}

/** The commit a branch of the double's repository points to. */
async function branchHead(orchestrator: Orchestrator, branch: string): Promise<string> {
  const refs = (await githubRepo(orchestrator, REPO)).refs;
  const sha = refs[`refs/heads/${branch}`] ?? refs[`heads/${branch}`];
  expect(sha, `no branch ${branch}`).toMatch(/^[0-9a-f]{40}$/);
  return sha!;
}

/**
 * The merge commit of a fix: the double's journal since `from` holds exactly one created commit with two parents (the
 * old head first, then the base tip), whose git ID is the branch's new head; the branch was updated without force.
 */
async function expectMergeCommit(
  orchestrator: Orchestrator,
  from: number,
  branch: string,
  oldHead: string,
  baseTip: string,
): Promise<string> {
  const calls = (await githubJournal(orchestrator)).slice(from);
  const merges = calls
    .filter((call) => call.route === CREATE_COMMIT && call.status === 201)
    .map((call) => call.body as CommitBody)
    .filter((body) => body.parents.length > 1);
  expect(merges, 'exactly one merge commit was created').toHaveLength(1);
  const merge = merges[0]!;
  expect(merge.parents).toEqual([oldHead, baseTip]);
  expect(merge.signature).toBeUndefined();
  const head = await branchHead(orchestrator, branch);
  expect(head).toBe(commitId(merge));
  // the push is a fast-forward of the old head (its first parent), never forced
  const updates = calls.filter((call) => call.route === UPDATE_REF && call.path.endsWith(`/heads/${branch}`));
  expect(updates.map((call) => [call.status, (call.body as { sha?: string; force?: boolean }).sha])).toEqual([
    [200, head],
  ]);
  expect((updates[0]!.body as { force?: boolean }).force ?? false).toBe(false);
  // nothing merged, closed or updated the pull request at GitHub
  expect(calls.filter((call) => /\/pulls\/\d+\/(merge|update-branch)$/.test(call.path))).toEqual([]);
  return head;
}

/** The row of a pull request (`owner/name#n`) in the visible pull-request list. */
const pullRow = (page: Page, label: string): Locator =>
  page.locator(`[data-testid="pulls.item"][data-pull="${label}"]`);

/**
 * Waits until the row's conflict state is `want`; records every state seen ("being checked" while the device read
 * `mergeable: null`, `state:checking` while a read is under way). "Check again" is pressed only when a read state
 * other than `want` stayed for more than 12 s (the view's own second read comes after 10 s).
 */
async function untilConflictState(row: Locator, want: 'conflict' | 'clean', seen: Set<string>): Promise<void> {
  let since = Date.now();
  await expect
    .poll(
      async () => {
        const conflict = (await row.getAttribute('data-conflict')) ?? '';
        const state = (await row.getAttribute('data-state')) ?? '';
        seen.add(conflict === '' ? `state:${state}` : conflict);
        const recheck = row.getByTestId('pulls.recheck');
        if (conflict !== want && Date.now() - since > 12_000 && (await recheck.isVisible())) {
          await recheck.click();
          since = Date.now();
        }
        return conflict;
      },
      { timeout: 150_000, intervals: [500] },
    )
    .toBe(want);
  await expect(row.getByTestId('pulls.conflict')).toHaveAttribute('data-conflict', want);
}

/** The Claude Code session ID the runner recorded for a session (null until the guest reported it). */
async function claudeId(orchestrator: Orchestrator, session: string): Promise<string | null> {
  const rows = await runnerStore<{ claude_session_id: string | null }>(
    orchestrator,
    `SELECT claude_session_id FROM runner.session WHERE id = '${uuid(session)}'`,
  );
  return rows[0]?.claude_session_id ?? null;
}

interface JournalRecord {
  readonly path: string;
  readonly step?: string | null;
  readonly body: unknown;
}

/** The newest request the model double answered with step `step` (its journal keeps the whole request body). */
async function requestOf(orchestrator: Orchestrator, step: string): Promise<string> {
  const { body } = await orchestrator.doubleRequest('model', 'GET', '/_double/journal');
  const records = (Array.isArray(body) ? body : []) as JournalRecord[];
  const found = records.filter((record) => record.path === '/v1/messages' && record.step === step).at(-1);
  expect(found, `no request answered by step ${step}`).toBeDefined();
  return JSON.stringify(found!.body);
}

/** Every main session of the task continued one Claude Code conversation (the first session's ID). */
async function expectOneConversation(orchestrator: Orchestrator, task: string, claude: string): Promise<void> {
  for (const session of await mainSessions(orchestrator, task)) {
    await expect.poll(() => claudeId(orchestrator, session.id), { timeout: 30_000 }).toBe(claude);
  }
}

type Json = Record<string, unknown>;

test.describe.configure({ mode: 'serial' });

test('@E2E-65 @needs-vm "Fix conflicts" lets the same session merge the base into the pull request with a merge commit, never merging it', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, world());
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, NAME);
  await connectGitHub(page, orchestrator, LOGIN);

  // 1. The task opens pull request #2 through the platform operations.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, OPENED);
  const [first] = await mainSessions(orchestrator, task);
  await expect.poll(() => claudeId(orchestrator, first!.id), { timeout: 60_000 }).toMatch(/^[0-9a-f-]{36}$/);
  const claude = (await claudeId(orchestrator, first!.id))!;
  let pulls = (await githubRepo(orchestrator, REPO)).pulls.map((pull) => [pull.number, pull.state, pull.head]);
  expect(pulls).toEqual([
    [1, 'open', FOREIGN],
    [2, 'open', HEAD],
  ]);
  const oldHead = await branchHead(orchestrator, HEAD);
  const foreignHead = await branchHead(orchestrator, FOREIGN);
  // The device registers the pull request's opaque link with the turn's context checkpoint (ADR-072 section 8), a
  // moment after the answer; the task's tab reads the list when it opens.
  await expect
    .poll(
      async () =>
        (
          await orchestrator.hubQuery<{ n: number }>(
            `SELECT count(*)::int AS n FROM hub.pr_link WHERE task_id = '${uuid(task)}' AND session_id IS NOT NULL`,
          )
        )[0]!.n,
      { timeout: 120_000, intervals: [1000] },
    )
    .toBe(1);

  // 2. The base advances with a change that conflicts with both pull requests; GitHub is still computing.
  const { sha: baseTip } = (
    await orchestrator.doubleRequest('github', 'POST', '/_double/commits', {
      repo: REPO,
      branch: 'main',
      commit: {
        message: 'Change the base.',
        files: {
          'NOTES.md': '# Notes\nkz61-base-notes-k8m3 from the base\n',
          'CHANGELOG.md': '# Changelog\n- 0.2 kz61-base-line-r2x6\n- 0.1 first release\n',
        },
      },
    })
  ).body as { sha: string };
  expect(baseTip).toMatch(/^[0-9a-f]{40}$/);
  expect(await branchHead(orchestrator, 'main')).toBe(baseTip);
  await armFaults(orchestrator, [
    { route: 'GET /repos/{owner}/{repo}/pulls/{number}', repo: REPO, kind: 'mergeable_unknown', times: 3 },
  ]);

  // 3. The task's tab: "being checked" first (never "no conflict"), then "conflict" with "Fix conflicts".
  await page.getByTestId('task.tabs.pulls').click();
  await expect(page.getByTestId('task.pulls')).toBeVisible({ timeout: 30_000 });
  const ours = pullRow(page, OURS);
  await expect(ours).toBeVisible({ timeout: 60_000 });
  const seen = new Set<string>();
  await untilConflictState(ours, 'conflict', seen);
  expect([...seen]).toContain('checking');
  expect([...seen]).not.toContain('clean');
  await expect(ours).toHaveAttribute('data-state', 'open');
  await expect(ours.getByTestId('pulls.fork')).toHaveCount(0);

  // 4. "Fix conflicts" sends the instruction v1 to the same task.
  const fixFrom = (await githubJournal(orchestrator)).length;
  await ours.getByTestId('pulls.fix').click();
  await expect(ours.getByTestId('pulls.fix.sent')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('task.tabs.chat').click();
  await expectAnswerInChat(page, FIXED);
  const prompts = await taskBodies(page, orchestrator, onboarded, task, 'prompt');
  expect(prompts.map((body) => body.text)).toEqual([PROMPT, instruction(REPO, 2)]);
  const resumed = await requestOf(orchestrator, 'fix-merge');
  for (const earlier of [PROMPT, 'Checking the repository out first.', 'kz61-head-notes-p4w9', OPENED]) {
    expect(resumed, `the fix request lacks ${earlier}`).toContain(earlier);
  }
  expect(resumed).toContain('fix merge conflicts');
  await expectOneConversation(orchestrator, task, claude);

  // 5. The operations of the fix: the prepared merge, the refused commit with markers, the merge commit, the push.
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state, op.error_code])).toEqual([
    ['github.checkout', 'succeeded', null],
    ['github.create_branch', 'succeeded', null],
    ['github.commit', 'succeeded', null],
    ['github.push', 'succeeded', null],
    ['github.create_pull_request', 'succeeded', null],
    ['github.merge_base', 'succeeded', null],
    ['github.commit', 'rejected', 'conflict_markers'],
    ['github.commit', 'succeeded', null],
    ['github.push', 'succeeded', null],
  ]);
  const results = new Map(
    (await taskBodies(page, orchestrator, onboarded, task, 'operation.result')).map((body) => [
      body.operation_id,
      body as Json,
    ]),
  );
  const prepared = results.get(operations[5]!.id) as { status: string; result: Json };
  expect(prepared.status).toBe('succeeded');
  expect(prepared.result).toMatchObject({
    repo: REPO,
    number: 2,
    branch: HEAD,
    base: 'main',
    head_sha: oldHead,
    base_sha: baseTip,
    up_to_date: false,
    conflicts: [{ path: 'NOTES.md', kind: 'content' }],
    conflicts_total: 1,
    dir: `repos/${REPO}`,
  });
  const refused = results.get(operations[6]!.id) as { status: string; error: { code: string; message?: string } };
  expect(refused.status).toBe('rejected');
  expect(refused.error.code).toBe('conflict_markers');
  if (refused.error.message !== undefined) expect(refused.error.message).toContain('NOTES.md');
  const marked = await requestOf(orchestrator, 'fix-write');
  for (const marker of [`<<<<<<< pull request (${HEAD})`, '||||||| common ancestor', '>>>>>>> base (main)']) {
    expect(marked, `the read file lacks ${marker}`).toContain(marker);
  }

  // 6. The branch points to the merge commit (old head, base tip), a fast-forward; the pull request stays open.
  const merge = await expectMergeCommit(orchestrator, fixFrom, HEAD, oldHead, baseTip);
  const pushed = results.get(operations[8]!.id) as { status: string; result: { sha: string } };
  expect(pushed.result.sha).toBe(merge);
  pulls = (await githubRepo(orchestrator, REPO)).pulls.map((pull) => [pull.number, pull.state, pull.head]);
  expect(pulls).toEqual([
    [1, 'open', FOREIGN],
    [2, 'open', HEAD],
  ]);
  await page.getByTestId('task.tabs.pulls').click();
  await expect(ours.getByTestId('pulls.fixed')).toHaveAttribute('data-commit', merge, { timeout: 60_000 });
  await untilConflictState(ours, 'clean', new Set());
  await expect(ours.getByTestId('pulls.fix')).toHaveCount(0);

  // 7a. The foreign pull request is linked to the task by pasting its address in the workgroup's view.
  await navTo(page, 'pulls');
  await expect(page.getByTestId('pulls.screen')).toBeVisible({ timeout: 30_000 });
  const form = page.getByTestId('pulls.link');
  await form.getByTestId('pulls.link.input').fill(THEIRS);
  const choice = form.getByTestId('pulls.link.task');
  await expect(choice.locator(`option[value="${task}"]`)).toHaveCount(1, { timeout: 30_000 });
  await choice.selectOption(task);
  await form.getByTestId('pulls.link.submit').click();
  await expect(form.getByTestId('pulls.link.created')).toBeVisible({ timeout: 60_000 });
  const theirs = pullRow(page, THEIRS);
  await expect(theirs).toBeVisible({ timeout: 60_000 });
  await expect(theirs).toHaveAttribute('data-task', task);
  await untilConflictState(theirs, 'conflict', new Set());
  await expect(theirs.getByTestId('pulls.unlink')).toBeVisible();
  await expect(pullRow(page, OURS)).toHaveAttribute('data-task', task);
  await expect(pullRow(page, OURS).getByTestId('pulls.unlink')).toHaveCount(0);

  // 8a. The hub holds both links as opaque tags with sealed bodies of fixed length: one of the device (the session
  // that created #2), one of the person who pasted #1.
  const links = await orchestrator.hubQuery<{ device: boolean; person: boolean; sealed: number }>(
    `SELECT session_id IS NOT NULL AS device, linked_by IS NOT NULL AS person, length(sealed)::int AS sealed
       FROM hub.pr_link WHERE task_id = '${uuid(task)}' ORDER BY created_at`,
  );
  expect(links).toEqual([
    { device: true, person: false, sealed: 379 },
    { device: false, person: true, sealed: 379 },
  ]);

  // 7b. The same through the agent tool: list, show, the existing link, and "fix conflicts" for the foreign one.
  await pairAgent(orchestrator, page, e2e.hubOrigin, HOME);
  const listed = await agentTool(orchestrator, HOME, ['pr', 'list', '--task', task], 180_000);
  const rows = (listed.links as Json[]).map((link) => [
    `${String(link.repo)}#${String(link.number)}`,
    link.task_id,
    link.origin,
    link.merge_state,
    (link.pull as Json | null)?.state,
    link.status_error,
  ]);
  expect(rows.sort()).toEqual([
    [THEIRS, task, 'person', 'conflict', 'open', null],
    [OURS, task, 'device', 'clean', 'open', null],
  ]);
  const shown = await agentTool(orchestrator, HOME, ['pr', 'show', THEIRS], 180_000);
  expect(shown).toMatchObject({ repo: REPO, number: 1, merge_state: 'conflict', fixable: true, status_error: null });
  expect((shown.links as Json[]).map((link) => [link.task_id, link.origin])).toEqual([[task, 'person']]);
  expect(shown.pull).toMatchObject({ state: 'open', merged: false, mergeable: false, head_ref: FOREIGN });
  const again = await agentTool(orchestrator, HOME, ['pr', 'link', THEIRS, '--task', task]);
  expect(again).toMatchObject({ created: false, link: { task_id: task, origin: 'person' } });
  const foreignFrom = (await githubJournal(orchestrator)).length;
  const sent = await agentTool(orchestrator, HOME, ['pr', 'fix-conflicts', THEIRS], 180_000);
  expect(sent).toMatchObject({ repo: REPO, number: 1, instruction_version: 1, merge_state: 'conflict' });
  expect(sent.instruction).toBe(instruction(REPO, 1));

  // 7c. The same session resolves the foreign pull request: merge commit on its branch, the pull request open.
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 420_000 }).toBe(true);
  await expect.poll(() => branchHead(orchestrator, FOREIGN), { timeout: 60_000 }).not.toBe(foreignHead);
  await expectMergeCommit(orchestrator, foreignFrom, FOREIGN, foreignHead, baseTip);
  const foreignRequest = await requestOf(orchestrator, 'foreign-checkout');
  // the request is JSON: the instruction's quotes and line feeds arrive escaped
  expect(foreignRequest).toContain(JSON.stringify(instruction(REPO, 1)).slice(1, -1));
  for (const earlier of [PROMPT, OPENED, FIXED, 'kz61-base-notes-k8m3']) {
    expect(foreignRequest, `the foreign fix request lacks ${earlier}`).toContain(earlier);
  }
  expect(await requestOf(orchestrator, 'foreign-write')).toContain(`<<<<<<< pull request (${FOREIGN})`);
  await expectOneConversation(orchestrator, task, claude);
  const all = await hubOperations(orchestrator, task);
  expect(all.slice(operations.length).map((op) => [op.type, op.state, op.error_code])).toEqual([
    ['github.checkout', 'succeeded', null],
    ['github.merge_base', 'succeeded', null],
    ['github.commit', 'rejected', 'conflict_markers'],
    ['github.commit', 'succeeded', null],
    ['github.push', 'succeeded', null],
  ]);
  pulls = (await githubRepo(orchestrator, REPO)).pulls.map((pull) => [pull.number, pull.state, pull.head]);
  expect(pulls).toEqual([
    [1, 'open', FOREIGN],
    [2, 'open', HEAD],
  ]);
  expect(await branchHead(orchestrator, HEAD)).toBe(merge);
  expect(await branchHead(orchestrator, 'main')).toBe(baseTip);

  // 8b. Zero knowledge: no repository, branch, login, file, title or prompt text at the hub side.
  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});
