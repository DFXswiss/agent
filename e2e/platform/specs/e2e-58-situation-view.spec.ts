// E2E-58 (KOR-39, ADR-076; 06-web.md section 4.0): the situation view of an organization's home with the whole chain.
// A real Claude Code session asks a person through the operation `human.question` and ends its turn: the task appears
// under "Needs your decision" with the agent's question (sealed in `operation.params`, opened in the browser), the
// owner gets one `task.attention` notification, and the answer typed in the situation view continues the same session.
// An approval asked with `human.approval` is given with one tap ("Approved.") and the agent goes on (a missing
// connection and refused operations are covered by the hub and web suites: GitHub operations are held, ADR-066). A main
// agent that handed work to a subagent and ended its turn shows under "Running" with the phase "Helpers are working"
// and its subagent in the expandable tree. The hub holds none of the texts (plaintext scan). Runs on T1.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
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
  readFileSync(new URL('../scenarios/e2e-58-situation-view.json', import.meta.url), 'utf8'),
);
const ASK = 'E2E58-ASK kz58-prompt-a8d2f5: design the logo';
const ANSWER = 'Blue kz58-answer-b5n2m7';
const QUESTION = 'Which colour shall the logo have? kz58-question-r3t8w1';
const APPROVE = 'E2E58-APPROVE kz58-approve-c4v9: publish the report';
const APPROVAL = 'May I publish the report? kz58-approval-t2k8';
const SUB = 'E2E58-SUB kz58-sub-e6y1: get a long review';
const NAME = 'Ada kz58-name-g7u3';
const CANARIES = [
  'kz58-prompt-a8d2f5',
  'kz58-answer-b5n2m7',
  'kz58-question-r3t8w1',
  'kz58-done-p6q3z9',
  'kz58-approve-c4v9',
  'kz58-approval-t2k8',
  'kz58-published-w9',
  'kz58-sub-e6y1',
  'kz58-purpose-h4',
  'kz58-name-g7u3',
];

/** The entry of `task` in the group `category` of the situation view. */
const entry = (page: Page, category: string, task: string) =>
  page.getByTestId(`attention.group.${category}`).locator(`[data-testid="attention.item"][data-task="${task}"]`);

/** The hub's `task.attention` notifications of a task (content-free rows). */
const attentionNotifications = (orchestrator: Orchestrator, task: string) =>
  orchestrator.hubQuery<{ ref: string }>(
    `SELECT ref FROM hub.notification WHERE task_id = '${uuid(task)}' AND kind = 'task.attention' ORDER BY created_at`,
  );

test.describe.configure({ mode: 'serial' });

test('@E2E-58 @R-6.6 @needs-vm the situation view shows where a person is needed and takes the answer in place', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, NAME);

  // 1. The agent asks a person and ends its turn.
  const asked = await sendPrompt(page, ASK);
  await expectAnswerInChat(page, 'I asked the person and wait for the answer.');
  await home(page);
  const question = entry(page, 'needs_decision', asked);
  await expect(question).toBeVisible({ timeout: 60_000 });
  await expect(question.getByTestId('attention.item.reason')).toHaveText('The agent has a question');
  await expect(question.getByTestId('attention.question')).toHaveText(QUESTION);
  await expect(page.getByTestId('attention.counter.mine')).toHaveAttribute('data-count', '1');
  await expect(page.getByTestId('shell.badge.attention').filter({ visible: true })).toHaveCount(1);
  await expect
    .poll(async () => (await attentionNotifications(orchestrator, asked)).map((row) => row.ref))
    .toEqual([expect.stringMatching(/^needs_decision:question:\d+$/)]);

  // 2. The answer from the situation view continues the same session.
  await question.getByTestId('attention.answer').fill(ANSWER);
  await question.getByTestId('attention.answer.send').click();
  await expect(entry(page, 'needs_decision', asked)).toHaveCount(0, { timeout: 60_000 });
  await expect(entry(page, 'done', asked)).toBeVisible({ timeout: 120_000 });
  expect(await mainSessions(orchestrator, asked)).toHaveLength(1);
  expect((await taskBodies(page, orchestrator, onboarded, asked, 'prompt')).map((body) => body.text)).toEqual([
    ASK,
    ANSWER,
  ]);
  expect((await attentionNotifications(orchestrator, asked)).length).toBe(1);

  // 3. The agent asks for an approval: "Approve" in the situation view sends exactly "Approved." and it goes on.
  const approve = await sendPrompt(page, APPROVE);
  await expectAnswerInChat(page, 'I wait for the approval.');
  await home(page);
  const approval = entry(page, 'needs_decision', approve);
  await expect(approval).toBeVisible({ timeout: 60_000 });
  await expect(approval.getByTestId('attention.item.reason')).toHaveText('The agent asks for your approval');
  await expect(approval.getByTestId('attention.question')).toHaveText(APPROVAL);
  await approval.getByTestId('attention.approve').click();
  await expect(entry(page, 'needs_decision', approve)).toHaveCount(0, { timeout: 60_000 });
  await expect(entry(page, 'done', approve)).toBeVisible({ timeout: 120_000 });
  expect((await taskBodies(page, orchestrator, onboarded, approve, 'prompt')).map((body) => body.text)).toEqual([
    APPROVE,
    'Approved.',
  ]);

  // 4. The main agent hands the work to a subagent and ends its turn: "Running", helpers working, the tree.
  const sub = await sendPrompt(page, SUB);
  await expectAnswerInChat(page, 'A helper reviews it; I am done for now.');
  await home(page);
  const running = entry(page, 'working', sub);
  await expect(running).toBeVisible({ timeout: 60_000 });
  await expect(running.getByTestId('attention.item.reason')).toHaveText('Helpers are working', { timeout: 30_000 });
  await running.getByTestId('attention.item.toggle').click();
  await expect(running.getByTestId('attention.tree.node')).toHaveCount(2);
  await expect(entry(page, 'done', sub)).toBeVisible({ timeout: 180_000 });

  await expectNoPlaintext(orchestrator, CANARIES);
});
