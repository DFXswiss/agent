// Steps of the GitHub and operation scenarios (SCN-04): the GitHub double through the orchestrator's generic double
// route (seed, faults, expectations, device-flow decisions, journal, repository inspection; the double runs on the
// runner device in T1 and T2), the connect flow and the workgroup allow-list in the browser, and the hub's operation
// records.
import { expect, type Page } from '@playwright/test';
import type { Orchestrator } from './orchestrator.ts';
import { uuid } from './platform.ts';

/** The https site the double names in its web links (orchestrator `GITHUB_HTML_URL`). */
export const GITHUB_SITE = 'https://github.example';

export interface GitHubCall {
  readonly seq: number;
  readonly method: string;
  readonly path: string;
  /** `METHOD template`, e.g. `GET /repos/{owner}/{repo}` (absent for an unknown route). */
  readonly route?: string;
  readonly status: number;
  readonly body: unknown;
}

async function control<T>(
  orchestrator: Orchestrator,
  method: 'GET' | 'POST',
  path: string,
  body?: unknown,
): Promise<T> {
  const answer = await orchestrator.doubleRequest('github', method, path, body);
  if (answer.status !== 200)
    throw new Error(`GitHub double ${method} ${path}: ${answer.status} ${JSON.stringify(answer.body)}`);
  return answer.body as T;
}

/** Empties the double (world, faults, expectations, grants, journal) and seeds it (`agent_doubles.github.repos`). */
export async function seedGitHub(orchestrator: Orchestrator, seed: Record<string, unknown>): Promise<void> {
  await orchestrator.resetDouble('github');
  await control(orchestrator, 'POST', '/_double/seed', seed);
}

/** Arms faults (`agent_doubles.github.faults`); each must fire before the run's final check. */
export const armFaults = (orchestrator: Orchestrator, faults: readonly Record<string, unknown>[]) =>
  control(orchestrator, 'POST', '/_double/faults', { faults });

/** Expectations: exactly `count` matching requests (a further one is a failure of the double). */
export const expectCalls = (orchestrator: Orchestrator, expectations: readonly Record<string, unknown>[]) =>
  control(orchestrator, 'POST', '/_double/expect', { expectations });

/** Every API and login call the double received since its reset, in order. */
export const githubJournal = (orchestrator: Orchestrator) =>
  control<GitHubCall[]>(orchestrator, 'GET', '/_double/journal');

/** The route of every call since `from` (`METHOD template`; `METHOD path` for an unknown route). */
export async function githubRoutes(orchestrator: Orchestrator, from = 0): Promise<string[]> {
  return (await githubJournal(orchestrator)).slice(from).map((call) => call.route ?? `${call.method} ${call.path}`);
}

/** Refs, pull requests and comments of a repository as the double holds them. */
export const githubRepo = (orchestrator: Orchestrator, repo: string) =>
  control<{
    refs: Record<string, string>;
    pulls: readonly Record<string, unknown>[];
    comments: readonly Record<string, unknown>[];
  }>(orchestrator, 'GET', `/_double/repos/${repo}`);

/** The double's state must be ok: every fault fired, every expectation met, nothing unexpected arrived. */
export async function expectGitHubOk(orchestrator: Orchestrator): Promise<void> {
  const state = await orchestrator.doubleState('github');
  expect(state.failures).toEqual([]);
  expect(state.ok).toBe(true);
}

/** One user with an installation on `account` covering `repositories`, and the repositories (branch `main`). */
export function worldOf(login: string, account: string, repositories: readonly Record<string, unknown>[]) {
  return {
    users: [{ login, id: 7001, name: 'Octo Tester', email: null }],
    installations: [
      {
        id: 41,
        account,
        account_type: 'Organization',
        repository_selection: 'selected',
        repositories: repositories.filter((repo) => repo.installed !== false).map((repo) => repo.full_name),
        users: [login],
      },
    ],
    repositories: repositories.map(({ installed: _installed, ...repo }) => repo),
  };
}

/** A repository with `main` (README.md) and optional further seed fields. */
export function repository(fullName: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    full_name: fullName,
    private: true,
    default_branch: 'main',
    description: null,
    branches: [{ name: 'main', commits: [{ message: 'Initial commit.', files: { 'README.md': 'hello\n' } }] }],
    ...extra,
  };
}

/**
 * Connects GitHub from the home composer (06-web.md section 8.1): the GitHub chip offers "Connect GitHub", the settings
 * flow sends `github.connect` to the platform device, which starts the device flow at the double; the user code shown
 * in the page is authorized at the double for `login`, the device stores the credential and the page shows the login.
 * Returns the user code and the verification link.
 */
export async function connectGitHub(
  page: Page,
  orchestrator: Orchestrator,
  login: string,
): Promise<{ userCode: string; verification: string }> {
  await page.getByTestId('composer.chip.github').click();
  await page.getByTestId('composer.github-connect').click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByTestId('github.disconnected')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('github.connect').click();
  await expect(page.getByTestId('github.start')).toBeEnabled({ timeout: 60_000 });
  await page.getByTestId('github.start').click();
  const code = page.getByTestId('github.code.value');
  await expect(code).toHaveText(/^[A-Z0-9]{4}-[A-Z0-9]{4}$/, { timeout: 60_000 });
  const userCode = (await code.textContent())!;
  const verification = (await page.getByTestId('github.code.open').getAttribute('href'))!;
  await control(orchestrator, 'POST', '/_double/device', { user_code: userCode, action: 'authorize', login });
  await expect(page.getByTestId('github.connected')).toHaveText(`Connected as ${login}`, { timeout: 60_000 });
  return { userCode, verification };
}

/** Saves the workgroup's repository allow-list on its settings screen (client-side navigation keeps the unlock). */
export async function allowRepositories(page: Page, org: string, wg: string, repositories: readonly string[]) {
  await page.evaluate((target) => {
    history.pushState({}, '', target);
    dispatchEvent(new PopStateEvent('popstate'));
  }, `/o/${org}/w/${wg}/settings`);
  await expect(page.getByTestId('wg-settings.repositories')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('wg-settings.repositories').fill(repositories.join('\n'));
  await page.getByTestId('wg-settings.save').click();
  await expect(page.getByTestId('wg-settings.saved')).toBeVisible();
  await expect(page.getByTestId('wg-settings.repositories')).toHaveValue(repositories.join('\n').toLowerCase());
}

export interface OperationRow {
  readonly id: string;
  readonly session_id: string;
  readonly type: string;
  readonly state: string;
  readonly error_code: string | null;
  readonly duplicate_of: string | null;
  readonly attempt: number;
  readonly params_event: string | null;
  readonly result_event: string | null;
}

/**
 * The hub's operation records of a task (oldest first) with the kinds of their params and result events (null when
 * the event is missing).
 */
export async function hubOperations(orchestrator: Orchestrator, task: string): Promise<OperationRow[]> {
  return orchestrator.hubQuery<OperationRow>(
    `SELECT o.id, o.session_id, o.type, o.state, o.error_code, o.duplicate_of, o.attempt,
            p.kind AS params_event, r.kind AS result_event
       FROM hub.operation o
       LEFT JOIN hub.event p ON p.id = o.params_event_id LEFT JOIN hub.event r ON r.id = o.result_event_id
      WHERE o.task_id = '${uuid(task)}' ORDER BY o.created_at, o.id`,
  );
}

/** The states an operation passed through at the hub, in order. */
export async function operationStates(orchestrator: Orchestrator, operation: string): Promise<string[]> {
  const rows = await orchestrator.hubQuery<{ state: string }>(
    `SELECT state FROM hub.operation_transition WHERE operation_id = '${uuid(operation)}' ORDER BY seq`,
  );
  return rows.map((row) => row.state);
}

/** `action` of every audit row about the operation (oldest first). */
export async function operationAudit(orchestrator: Orchestrator, operation: string): Promise<string[]> {
  const rows = await orchestrator.hubQuery<{ action: string }>(
    `SELECT action FROM hub.audit WHERE target_id = '${uuid(operation)}' ORDER BY seq`,
  );
  return rows.map((row) => row.action);
}

/** Navigates the page into the task's Operations tab and returns the cards shown there. */
export async function openOperationsTab(page: Page) {
  await page.getByTestId('task.tabs.operations').click();
  return page.getByTestId('operation.card');
}
