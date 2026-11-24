// E2E-56 (07-testing.md section 2.1; rule E-29, ADR-073; 03-api.md section 9; 05-runner.md sections 11.4 and 12):
// GitHub works with every repository the user's account can reach, and a repository it does not reach ends with one
// next step that goes on by itself.
// - The fork case with the GitHub App only (the run's default device): the App is not installed on the user's own
//   account, so the session's request for the fork is held; the card in the chat names the one step (create a token,
//   pre-filled link) and the user stores a token right in the card; the operation goes on without a new prompt.
// - With the OAuth App set up on the device (restarted with `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_APP_SLUG`): "Connect
//   GitHub" signs in through the OAuth App (scopes `repo workflow`), and one session reads repositories in three places
//   - the own account and its fork, an organisation with the GitHub App, an organisation of a third party without an
//   installation - each at once.
// - An organisation that restricts OAuth Apps: held, the card names "request the approval" with GitHub's page of the
//   OAuth App; after the owner approved it at GitHub the runner checks again and the operation goes on without a new
//   prompt. In the mobile project the card fits the phone.
// Runs on T1.
import { readFileSync } from 'node:fs';
import type { Locator, Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  connectGitHub,
  expectGitHubOk,
  githubJournal,
  githubRoutes,
  hubOperations,
  repository,
  seedGitHub,
  storeGitHubToken,
} from '../fixtures/github.ts';
import { MD } from '../fixtures/nav.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { layoutProblems } from '../../../web/e2e/helpers/layout-checks.ts';

const LOGIN = 'kz56-octo';
const TOKEN = 'kz56-classic-token-w3n8';
const OAUTH_CLIENT_ID = 'Ov23double00000000';
const APP_SLUG = 'kz56-agent-app';
const RUNNER = 'runner-1';
const OWN = `${LOGIN}/tool`;
const FORK = `${LOGIN}/app-fork`;
const APP_ORG = 'kz56-app-org/service';
const THIRD = 'kz56-third-org/app';
const STRICT = 'kz56-strict-org/app';

const scenarioOf = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));

/** A repository with one issue whose title is `canary`. */
const withIssue = (name: string, canary: string) =>
  repository(name, { issues: [{ title: `Issue ${canary}`, user: LOGIN, body: null }] });

/** The user (collaborator in the organisations' repositories, with a classic token for the card), five repositories
 * in four places, the GitHub App installed on one organisation only, and an organisation that restricts OAuth Apps. */
function world(): Record<string, unknown> {
  return {
    users: [
      {
        login: LOGIN,
        id: 7056,
        name: 'Octo Tester',
        email: null,
        collaborator: [APP_ORG, THIRD, STRICT],
        tokens: [{ token: TOKEN, kind: 'classic', scopes: ['repo', 'workflow'] }],
      },
    ],
    repositories: [
      withIssue(OWN, 'kz56-own-a1q7'),
      withIssue(FORK, 'kz56-fork-b2w8'),
      withIssue(APP_ORG, 'kz56-org-c3e9'),
      withIssue(THIRD, 'kz56-third-d4r1'),
      withIssue(STRICT, 'kz56-strict-e5t2'),
    ],
    installations: [
      {
        id: 56,
        account: 'kz56-app-org',
        account_type: 'Organization',
        repository_selection: 'selected',
        repositories: [APP_ORG],
        users: [LOGIN],
      },
    ],
    organizations: [{ login: 'kz56-strict-org', oauth_restricted: true }],
  };
}

/** The run's platform device restarted with the GitHub ways `github` (same home: identity, store and VMs stay). */
async function restartRunner(orchestrator: Orchestrator, github: { oauth_client_id?: string; app_slug?: string }) {
  await orchestrator.stopRunner(RUNNER);
  await orchestrator.startRunner(RUNNER, github);
}

/** The one card of the task's chat, open and addressed to the viewer. */
async function openCard(page: Page, reason: string): Promise<Locator> {
  const card = page.getByTestId('waiting.card');
  await expect(card).toHaveCount(1, { timeout: 90_000 });
  await expect(card).toHaveAttribute('data-reason', reason);
  await expect(card).toHaveAttribute('data-status', 'open');
  await expect(card).toHaveAttribute('data-owner', 'true');
  if (page.viewportSize()!.width < MD) expect(await layoutProblems(page)).toEqual([]);
  return card;
}

test.describe.configure({ mode: 'serial' });

test.afterAll(async ({ orchestrator }) => {
  // the next spec finds the run's default device again (GitHub App only)
  await orchestrator.stopRunner(RUNNER);
  await orchestrator.startRunner(RUNNER, {});
});

test('@E2E-56 @R-9.1 @R-9.4 @needs-vm the fork case with the GitHub App only is held with one next step and goes on with a token from the card', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenarioOf('e2e-56-fork-app'));
  await seedGitHub(orchestrator, world());
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada kz56-name-fork');
  // the GitHub App's sign-in: its token reaches only owners where the App is installed (not the own account)
  await connectGitHub(page, orchestrator, LOGIN);
  const signIn = (await githubJournal(orchestrator)).find((call) => call.path === '/login/device/code')!;
  expect(String(signIn.body)).toContain('client_id=Iv1.0000double0000');

  await home(page);
  const task = await sendPrompt(page, `E2E56-FORK kz56-prompt-fork: read the newest issue of ${FORK}`);
  const card = await openCard(page, 'github_access');
  await expect(card.getByTestId('waiting.title')).toHaveText('GitHub access needed for the repository');
  await expect(card.getByTestId('waiting.repo')).toHaveText(FORK);
  const step = card.getByTestId('waiting.step');
  await expect(step).toHaveAttribute('data-action', 'new_token');
  await expect(card.getByTestId('waiting.step.link')).toHaveAttribute(
    'href',
    /\/settings\/tokens\/new\?scopes=repo%2Cworkflow&description=agent\+kz56-octo$/,
  );
  await expect(card.getByTestId('waiting.step.link')).toHaveAttribute('target', '_blank');
  expect(await hubOperations(orchestrator, task)).toEqual([]);

  // The user stores a token right in the card: the runner checks the fork again with it and goes on.
  await storeGitHubToken(card, TOKEN);
  await expectAnswerInChat(page, 'The fork answered with the token kz56-answer-fork-h9v3.');
  await expect(page.getByTestId('waiting.card')).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
  const prompts = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.event WHERE task_id = '${uuid(task)}' AND kind = 'prompt'`,
  );
  expect(prompts).toEqual([{ n: 1 }]);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expectGitHubOk(orchestrator);
});

test('@E2E-56 @R-9.1 @R-9.2 @R-9.4 @needs-vm one sign-in through the OAuth App reaches the own account, a fork, an organisation with the App and a third party organisation', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  await restartRunner(orchestrator, { oauth_client_id: OAUTH_CLIENT_ID, app_slug: APP_SLUG });
  await orchestrator.loadScenario('model', scenarioOf('e2e-56-every-repository'));
  await seedGitHub(orchestrator, world());
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada kz56-name-every');
  await connectGitHub(page, orchestrator, LOGIN);
  const signIn = (await githubJournal(orchestrator)).find((call) => call.path === '/login/device/code')!;
  expect(String(signIn.body)).toContain(`client_id=${OAUTH_CLIENT_ID}`);
  expect(String(signIn.body)).toContain('scope=repo+workflow');
  const from = (await githubJournal(orchestrator)).length;

  await home(page);
  await sendPrompt(page, 'E2E56-EVERY kz56-prompt-every: the newest issue of each of my repositories');
  await expectAnswerInChat(page, 'All four repositories answered kz56-answer-every-x7p2.');
  await expect(page.getByTestId('waiting.card')).toHaveCount(0);
  const routes = await githubRoutes(orchestrator, from);
  for (const repo of [OWN, FORK, APP_ORG, THIRD]) {
    const [owner, name] = repo.split('/');
    expect((await githubJournal(orchestrator)).some((call) => call.path === `/repos/${owner}/${name}/issues`)).toBe(
      true,
    );
  }
  expect(routes.filter((route) => route.includes('/user/installations'))).toEqual([]);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expectGitHubOk(orchestrator);
});

test('@E2E-56 @R-9.1 @R-9.4 @needs-vm an organisation that restricts OAuth Apps is held with the approval link and goes on after the approval', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenarioOf('e2e-56-restricted'));
  await seedGitHub(orchestrator, world());
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada kz56-name-strict');
  await connectGitHub(page, orchestrator, LOGIN);

  await home(page);
  const task = await sendPrompt(page, `E2E56-STRICT kz56-prompt-strict: read the newest issue of ${STRICT}`);
  const card = await openCard(page, 'github_access');
  await expect(card.getByTestId('waiting.repo')).toHaveText(STRICT);
  await expect(card.getByTestId('waiting.step')).toHaveAttribute('data-action', 'approve_app');
  const link = card.getByTestId('waiting.step.link');
  await expect(link).toHaveText('Request the approval of kz56-strict-org');
  await expect(link).toHaveAttribute(
    'href',
    new RegExp(`^http://127\\.0\\.0\\.1:\\d+/settings/connections/applications/${OAUTH_CLIENT_ID}$`),
  );
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(await hubOperations(orchestrator, task)).toEqual([]);

  // An owner of the organisation approves the OAuth App at GitHub; nothing reaches the platform but the next check.
  const approved = await orchestrator.doubleRequest('github', 'POST', '/_double/organizations', {
    login: 'kz56-strict-org',
    oauth_approved: [OAUTH_CLIENT_ID],
  });
  expect(approved.status).toBe(200);
  await expectAnswerInChat(page, 'The restricted organisation answered after the approval kz56-answer-strict-k4m8.');
  await expect(page.getByTestId('waiting.card')).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
  const prompts = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.event WHERE task_id = '${uuid(task)}' AND kind = 'prompt'`,
  );
  expect(prompts).toEqual([{ n: 1 }]);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expectGitHubOk(orchestrator);
});
