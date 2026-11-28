// E2E-53 (HOT-14, ADR-070; 05-runner.md section 12 step 3; 03-api.md sections 9, 10): a personal connection is never
// used on behalf of another person. Ada owns a task and has connected her own GitHub account; Wendy (member, write
// access to the default workgroup, no GitHub) writes in Ada's task and asks for a comment. The runner holds the
// operation for Wendy: the waiting card offers the connect controls to Wendy only and shows Ada whom it waits for,
// the waiting event names Wendy (`for_user`), and the GitHub double receives no request at all - Ada's token is never
// used. Wendy stores her own token in the card: the held comment runs with Wendy's account. Ada writes again and asks
// for another comment: it runs with Ada's account. The `operation.result` events name whose connection each operation
// used (`connection_of`). Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  connectGitHubToken,
  expectGitHubOk,
  githubRepo,
  githubRoutes,
  repository,
  seedGitHub,
  storeGitHubToken,
} from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  openChat,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
} from '../fixtures/platform.ts';
import { inviteMember, joinAsNewUser } from '../fixtures/team.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-53-shared-task-github.json', import.meta.url), 'utf8'),
);
const SHARED = 'octo-org/shared';
const ADA_LOGIN = 'kz53-ada-h3v8';
const WENDY_LOGIN = 'kz53-wendy-p8d2';
/** Not GitHub-shaped on purpose: tokens of the double only. */
const ADA_TOKEN = ['kz53', 'ada', 'tok', 'm4q7z1c9'].join('-');
const WENDY_TOKEN = ['kz53', 'wendy', 'tok', 'r6e2u8b3'].join('-');
const WENDY_NAME = 'Wendy kz53-name-w4';
const FIRST = 'E2E51-ADA-FIRST kz53-prompt-a-g2s5: get ready for the review of the shared issue';
const WENDY_PROMPT = `E2E51-WENDY kz53-prompt-w-n7f3: comment on issue 1 in ${SHARED} that I reviewed it`;
const AGAIN = `E2E51-ADA-AGAIN kz53-prompt-d-y1h4: comment on issue 1 in ${SHARED} that I approve it`;
const WENDY_COMMENT = 'Reviewed by Wendy kz53-comment-w-t6r2';
const ADA_COMMENT = 'Approved by Ada kz53-comment-a-b5n7';

const classic = (login: string, id: number, token: string) => ({
  login,
  id,
  name: null,
  email: null,
  tokens: [{ token, kind: 'classic', scopes: ['repo', 'read:org'] }],
});

test.describe.configure({ mode: 'serial' });

test("@E2E-53 @needs-vm in a shared task every GitHub operation runs with the account of the person who asked for it, never the owner's for someone else", async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, {
    users: [classic(ADA_LOGIN, 7001, ADA_TOKEN), classic(WENDY_LOGIN, 7002, WENDY_TOKEN)],
    repositories: [repository(SHARED, { issues: [{ title: 'Shared issue kz53-issue-k3w9', user: ADA_LOGIN }] })],
  });
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz53-name-a1');
  const org = onboarded.org;
  const ada = await PageUser.open(page, onboarded.recoveryCode);

  // Ada connects her own account; Wendy joins with write access to the default workgroup and connects nothing.
  await connectGitHubToken(page, ADA_TOKEN, ADA_LOGIN);
  const wendy = await joinAsNewUser(browser, e2e, org, await inviteMember(page, org, {}), WENDY_NAME);
  await expect(wendy.page.getByTestId('bell.link')).toBeVisible();

  // Ada's task, answered with text.
  await home(page);
  const task = await sendPrompt(page, FIRST);
  await expectAnswerInChat(page, 'Ready for the shared work kz53-answer-a-q4m8.');
  const beforeWendy = (await githubRoutes(orchestrator)).length;

  // Wendy writes in Ada's task and asks for a comment: held for Wendy, not run with Ada's account.
  await navigateInApp(wendy.page, `/o/${org}/t/${task}`);
  await openChat(wendy.page); // the chat is the second tab (KONZEPT E-43)
  await wendy.page.getByTestId('chat.composer.prompt').fill(WENDY_PROMPT);
  await wendy.page.getByTestId('chat.composer.send').click();
  const wendyCard = wendy.page.getByTestId('waiting.card');
  await expect(wendyCard).toHaveCount(1, { timeout: 90_000 });
  await expect(wendyCard).toHaveAttribute('data-status', 'open');
  await expect(wendyCard).toHaveAttribute('data-owner', 'true');
  await expect(wendyCard.getByTestId('github.token')).toBeVisible();
  await expect(wendyCard.getByTestId('waiting.repo')).toHaveText(SHARED);
  const adaCard = page.getByTestId('waiting.card');
  await expect(adaCard).toHaveCount(1, { timeout: 30_000 });
  await expect(adaCard).toHaveAttribute('data-owner', 'false');
  await expect(adaCard.getByTestId('github.token')).toHaveCount(0);
  await expect(adaCard.getByTestId('waiting.other')).toHaveText(`Waiting for ${WENDY_NAME} to connect GitHub.`, {
    timeout: 30_000,
  });
  expect(await githubRoutes(orchestrator, beforeWendy)).toEqual([]);

  // Wendy connects her own account right in the card: the held comment runs with it.
  await storeGitHubToken(wendyCard, WENDY_TOKEN);
  await expect(wendyCard).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
  await expectAnswerInChat(wendy.page, "Wendy's comment is posted kz53-answer-w-c9j3.");
  const commentsBy = async (body: string) =>
    (await githubRepo(orchestrator, SHARED)).comments.filter((comment) => comment.body === body).map((c) => c.user);
  expect(await commentsBy(WENDY_COMMENT)).toEqual([WENDY_LOGIN]);

  // Ada writes again and asks for a comment: it runs at once with Ada's account.
  await page.getByTestId('chat.composer.prompt').fill(AGAIN);
  await page.getByTestId('chat.composer.send').click();
  await expectAnswerInChat(page, "Ada's comment is posted kz53-answer-d-x2k6.");
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  expect(await commentsBy(ADA_COMMENT)).toEqual([ADA_LOGIN]);

  // The task's record: one waiting event, addressed to Wendy; the results name whose connection was used.
  const waiting = (await taskBodies(page, orchestrator, onboarded, task, 'session.status')).filter(
    (body) => body.state === 'waiting',
  );
  expect(waiting).toHaveLength(1);
  expect(waiting[0]).toMatchObject({ waiting_for: 'github_connection', for_user: wendy.user.userId, repo: SHARED });
  const results = await taskBodies(page, orchestrator, onboarded, task, 'operation.result');
  expect(results.map((body) => [body.status, body.connection_of])).toEqual([
    ['succeeded', wendy.user.userId],
    ['succeeded', ada.userId],
  ]);

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, [ADA_TOKEN, WENDY_TOKEN, WENDY_COMMENT, ADA_COMMENT, 'kz53-prompt-w-n7f3']);
  await wendy.page.context().close();
});
