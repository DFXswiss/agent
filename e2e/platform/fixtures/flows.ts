// Whole flows that the system-wide checks repeat (SCN-09): the first task of E2E-06 and the GitHub operation chain of
// E2E-21, with the assertions of those specs, parameterised so E2E-09 can run them with the run's canaries and E2E-32
// can run them as they are across the role topology. Plus the canary helpers of E2E-09 (`routes/canary.py`).
import type { Page } from '@playwright/test';
import { expect } from '../fixtures/test.ts';
import {
  allowRepositories,
  connectGitHub,
  expectGitHubOk,
  githubRepo,
  githubRoutes,
  hubOperations,
  operationAudit,
  operationStates,
  repository,
  seedGitHub,
  worldOf,
} from './github.ts';
import type { Orchestrator } from './orchestrator.ts';
import {
  expectAnswerInChat,
  expectStartOrder,
  home,
  mainSessions,
  type Onboarded,
  sendPrompt,
  transitions,
  uuid,
} from './platform.ts';

// -- canaries ---------------------------------------------------------------------------------------------------------

export interface CanaryPair {
  readonly ascii: string;
  readonly unicode: string;
}

/** Fresh canary pairs per label (random per run) at the orchestrator, which keeps them for the scans. */
export async function newCanaries<L extends string>(
  orchestrator: Orchestrator,
  labels: readonly L[],
): Promise<Record<L, CanaryPair>> {
  return orchestrator.call<Record<L, CanaryPair>>('POST', '/canary/new', { labels });
}

/** Values met on the way (invitation and recovery codes) that must not reach the hub either. */
export async function addCanaryValues(orchestrator: Orchestrator, values: Readonly<Record<string, string>>) {
  await orchestrator.call<unknown>('POST', '/canary/new', { values });
}

/** Both canaries of a pair in one text (a field gets an ASCII and a non-ASCII canary). */
export const both = (pair: CanaryPair): string => `${pair.ascii} ${pair.unicode}`;

/** `scenario` with every `@@NAME@@` in its strings replaced by `values[NAME]`; an unknown name throws. */
export function fill(scenario: unknown, values: Readonly<Record<string, string>>): unknown {
  return JSON.parse(JSON.stringify(scenario), (_key, value: unknown) =>
    typeof value === 'string'
      ? value.replace(/@@([A-Z_]+)@@/g, (_all, name: string) => {
          const text = values[name];
          if (text === undefined) throw new Error(`no value for @@${name}@@`);
          return text;
        })
      : value,
  );
}

// -- E2E-06: first task -----------------------------------------------------------------------------------------------

/**
 * The first task of a new private org on the warm pool (E2E-06): countdown, session transitions in order,
 * `first_working_at − created_at < 10 s`, `agent_kind = claude-code`, the answer in the chat. The model double's
 * scenario must be loaded. Returns the task ID.
 */
export async function firstTask(
  page: Page,
  orchestrator: Orchestrator,
  device: string,
  prompt: string,
  answer: string,
): Promise<string> {
  const task = await sendPrompt(page, prompt);
  await expect(page.getByTestId('countdown.panel').first()).toBeVisible();
  await expect(page.getByTestId('countdown.summary')).toHaveText(/^Ready in \d+\.\d s$/, { timeout: 60_000 });
  const [session] = await mainSessions(orchestrator, task);
  expect(session).toMatchObject({ device_id: device, agent_kind: 'claude-code' });
  expect(session!.seconds).not.toBeNull();
  expect(session!.seconds!).toBeLessThan(10);
  expectStartOrder(await transitions(orchestrator, session!.id));
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expect(page.getByTestId('chat.prompt.text').first()).toHaveText(prompt);
  await expectAnswerInChat(page, answer);
  return task;
}

// -- E2E-21: GitHub operation chain -----------------------------------------------------------------------------------

export const GITHUB_TYPES = [
  'github.list_repositories',
  'github.checkout',
  'github.create_branch',
  'github.commit',
  'github.push',
  'github.create_pull_request',
  'github.comment',
  'github.get_pull_request',
] as const;

/** The REST calls of the chain at the double, in order (the pre-checks and executors of 05-runner.md section 12.1). */
export const GITHUB_REST_SEQUENCE = [
  'GET /user/installations',
  'GET /user/installations/{installation_id}/repositories',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/commits/{ref}',
  'GET /repos/{owner}/{repo}/tarball/{ref}',
  'GET /codeload/{owner}/{repo}/legacy.tar.gz/{ref}',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'POST /repos/{owner}/{repo}/git/refs',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'GET /repos/{owner}/{repo}/git/commits/{sha}',
  'GET /repos/{owner}/{repo}/git/trees/{tree}',
  'POST /repos/{owner}/{repo}/git/blobs',
  'POST /repos/{owner}/{repo}/git/trees',
  'POST /repos/{owner}/{repo}/git/commits',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'GET /repos/{owner}/{repo}/compare/{basehead}',
  'PATCH /repos/{owner}/{repo}/git/refs/{ref}',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/pulls',
  'POST /repos/{owner}/{repo}/pulls',
  'GET /repos/{owner}/{repo}',
  'POST /repos/{owner}/{repo}/issues/{number}/comments',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/pulls/{number}',
  'GET /repos/{owner}/{repo}/pulls/{number}/files',
  'GET /repos/{owner}/{repo}/issues/{number}/comments',
];

export interface GitHubChain {
  /** GitHub login of the user (the double's world). */
  readonly login: string;
  /** `owner/name` of the repository; `owner` is the installation account. */
  readonly repo: string;
  readonly branch: string;
  readonly prompt: string;
  /** The session's last answer (shown in the chat). */
  readonly answer: string;
  /** The comment the session writes on the pull request. */
  readonly comment: string;
}

/**
 * The GitHub operation chain of E2E-21 for the signed-in `onboarded` user: GitHub connected (device flow at the
 * double), the repository on the default workgroup's allow-list, the prompt sent, the scenario's chain run against
 * the double. Asserted: the exact REST sequence, the draft pull request with the comment, one succeeded hub record per
 * operation with its params and result events, transitions and audit rows, all executed by `device`. The model double's
 * scenario must be loaded. Returns the task ID.
 */
export async function githubChain(
  page: Page,
  orchestrator: Orchestrator,
  onboarded: Onboarded,
  device: string,
  chain: GitHubChain,
): Promise<string> {
  await seedGitHub(orchestrator, worldOf(chain.login, chain.repo.split('/')[0]!, [repository(chain.repo)]));
  await home(page);
  await connectGitHub(page, orchestrator, chain.login);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [chain.repo]);
  const connectCalls = (await githubRoutes(orchestrator)).length;

  await home(page);
  const task = await sendPrompt(page, chain.prompt);
  await expectAnswerInChat(page, chain.answer);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);

  expect(await githubRoutes(orchestrator, connectCalls)).toEqual(GITHUB_REST_SEQUENCE);
  const repo = await githubRepo(orchestrator, chain.repo);
  const pulls = repo.pulls.map((pull) => [pull.number, pull.state, pull.draft, pull.head, pull.base]);
  expect(pulls).toEqual([[1, 'open', true, chain.branch, 'main']]);
  expect(repo.comments.map((comment) => comment.body)).toEqual([chain.comment]);

  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state, op.session_id, op.params_event, op.result_event])).toEqual(
    GITHUB_TYPES.map((type) => [type, 'succeeded', session!.id, 'operation.params', 'operation.result']),
  );
  for (const op of operations) {
    expect(await operationStates(orchestrator, op.id)).toEqual(['received', 'accepted', 'executing', 'succeeded']);
    // change rows only: a browser that opened the operation meanwhile adds a `read.operation` row (read log, R-8.9)
    const changes = (await operationAudit(orchestrator, op.id)).filter((action) => !action.startsWith('read.'));
    expect(changes).toEqual(['operation.accepted', 'operation.executing', 'operation.succeeded']);
  }
  const devices = await orchestrator.hubQuery<{ device_id: string }>(
    `SELECT DISTINCT device_id FROM hub.operation WHERE task_id = '${uuid(task)}'`,
  );
  expect(devices).toEqual([{ device_id: device }]);
  await expectGitHubOk(orchestrator);
  return task;
}
