// E2E-69 (07-testing.md section 2.1; KOR-51, ADR-094; concept idea 42, interpretation A1, rule E-11): agent roles and
// rights profiles keep work, review and release apart - voluntarily, never as a precondition.
// 1. The workgroup's settings list the built-in roles; Ada sets the merge rules there (one passed verdict of a review
//    session, separate duties).
// 2. Her task (no profile: every right) starts a development child that checks out, writes, commits, pushes and opens
//    pull request #1; the same child asks to approve its own head with `review.verdict` and is refused
//    (`rights_denied`, audited).
// 3. A review child (profile `review`) reads the reviews and gives a passed verdict on that head; a merge child
//    (profile `merge`) requests `github.merge`: the criteria hold (the verdict counts, the writer is another session),
//    and the double merges the pull request at exactly that head.
// 4. A child without a profile may do what its parent may (`task.set_title`); a read-only child whose prompt tells it
//    to ignore its profile is refused its comment (by the guest, registered `rejected`) and a child with more rights.
// 5. The sessions carry their profiles at the hub and their roles in the session tree; the hub holds no repository,
//    branch, file, title or prompt text (plaintext scan).
// Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  connectGitHub,
  githubJournal,
  githubRepo,
  hubOperations,
  repository,
  seedGitHub,
  worldOf,
} from '../fixtures/github.ts';
import { navigate } from '../fixtures/identity.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-69-agent-roles.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz69-octo-p3m7';
const OWNER = 'kz69-org';
const REPO = `${OWNER}/kz69-app-r4t8`;
const NAME = 'Ada kz69-name-j2h4';
const PROMPT = `E2E69-ROLES kz69-prompt-c1v9: change ${REPO} with separate roles`;
const ANSWER = 'All roles done kz69-answer-e6r1.';
const CANARIES = [
  LOGIN,
  REPO,
  'kz69-app-r4t8',
  'kz69-change-v2k5',
  'kz69-name-j2h4',
  'kz69-prompt-c1v9',
  'kz69-notes-line-q8w3',
  'kz69-title-z5x1',
  'kz69-comment-y7u2',
  'kz69-purpose-dev',
  'kz69-purpose-review',
  'kz69-purpose-merge',
  'kz69-purpose-reader',
  'kz69-task-title',
];
const MERGE = 'PUT /repos/{owner}/{repo}/pulls/{number}/merge';

test('@E2E-69 @needs-vm agent roles keep work, review and release apart, restrict only by choice and never exceed the parent', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_800_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, OWNER, [repository(REPO)]));
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, NAME);
  await connectGitHub(page, orchestrator, LOGIN);

  // 1. The roles and the merge rules of the workgroup.
  const [wg] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT id FROM hub.wg WHERE org_id = '${uuid(onboarded.org)}' AND is_default`,
  );
  await navigate(page, `/o/${onboarded.org}/w/${wg!.id}/settings`);
  const roles = page.getByTestId('wg-settings.profile');
  await expect(roles).toHaveCount(5, { timeout: 30_000 });
  expect(await roles.evaluateAll((items) => items.map((item) => item.getAttribute('data-name')))).toEqual([
    'development',
    'merge',
    'read_only',
    'review',
    'security',
  ]);
  await page.getByTestId('wg-settings.merge.verdicts').fill('1');
  await page.getByTestId('wg-settings.merge.separate').check();
  await page.getByTestId('wg-settings.save').click();
  await expect(page.getByTestId('wg-settings.saved')).toBeVisible({ timeout: 30_000 });

  // 2.-4. The chain of roles.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expect(page.getByTestId('chat.output').filter({ hasText: ANSWER })).toBeVisible({ timeout: 1_200_000 });
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

  const operations = (await hubOperations(orchestrator, task)).map((op) => [op.type, op.state, op.error_code]);
  expect(operations).toEqual(
    expect.arrayContaining([
      ['github.create_pull_request', 'succeeded', null],
      ['review.verdict', 'rejected', 'rights_denied'],
      ['review.verdict', 'succeeded', null],
      ['github.merge', 'succeeded', null],
      ['task.set_title', 'succeeded', null],
      ['github.comment', 'rejected', 'rights_denied'],
    ]),
  );
  const requests = operations.filter(([type]) => type === 'subagent.request');
  expect(requests.map(([, state, code]) => [state, code])).toEqual([
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['rejected', 'rights_denied'],
  ]);
  const refusals = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.audit WHERE task_id = '${uuid(task)}' AND action = 'operation.rejected'
       AND detail->>'error_code' = 'rights_denied'`,
  );
  expect(refusals[0]!.n).toBe(3);

  // 5. The profiles at the hub, the merge in the double and the roles in the session tree.
  const sessions = await orchestrator.hubQuery<{ depth: number; profile: string | null }>(
    `SELECT depth, agent_profile->>'name' AS profile FROM hub.session WHERE task_id = '${uuid(task)}'
      ORDER BY created_at, id`,
  );
  expect(sessions.map((row) => [row.depth, row.profile])).toEqual([
    [0, null],
    [1, 'development'],
    [1, 'review'],
    [1, 'merge'],
    [1, null],
    [1, 'read_only'],
  ]);
  const pulls = (await githubRepo(orchestrator, REPO)).pulls.map((pull) => [pull.number, pull.state]);
  expect(pulls).toEqual([[1, 'closed']]);
  const merges = (await githubJournal(orchestrator)).filter((call) => call.route === MERGE);
  expect(merges.map((call) => call.status)).toEqual([200]);
  await page.getByTestId('task.tabs.sessions').click();
  const badges = page.getByTestId('session.role');
  await expect(badges).toHaveCount(4, { timeout: 30_000 });
  expect(await badges.evaluateAll((items) => items.map((item) => item.getAttribute('data-role')).sort())).toEqual([
    'development',
    'merge',
    'read_only',
    'review',
  ]);
  await navigate(page, `/o/${onboarded.org}/t/${task}`);
  await expect(page.getByTestId('task.profile.name')).toHaveAttribute('data-profile', '');

  await expectNoPlaintext(orchestrator, CANARIES);
  await expectAnswerInChat(page, ANSWER);
});
