// E2E-08 (07-testing.md section 2.1, KONZEPT section 2): every request is a task. Two knowledge questions from the
// home screen become two tasks with distinct IDs, derived titles and the workgroup set; a follow-up prompt of the
// first task stays in that task and reaches its session; there is no project object in the API, the schema or the UI.
// Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
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
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-08-every-request-task.json', import.meta.url), 'utf8'),
);

test.describe.configure({ mode: 'serial' });

test('@E2E-08 @R-0.2 @R-2.6 @R-2.7 @R-2.8 @R-2.9 @R-2.10 @R-P.5 @needs-vm two questions are two tasks and a follow-up stays in its task', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(420_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada Lovelace');
  const outputs = async (task: string) =>
    (await taskBodies(page, orchestrator, onboarded, task, 'session.output')).map((body) => body.text);
  const texts = async (task: string, kind: string, field: string) =>
    (await taskBodies(page, orchestrator, onboarded, task, kind)).map((body) => body[field]);

  const first = await sendPrompt(page, 'What is the capital of France?');
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in', { timeout: 60_000 });
  await expectAnswerInChat(page, 'Paris.');

  await home(page);
  const second = await sendPrompt(page, 'Who wrote Hamlet?\n\nOne line is enough.');
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in', { timeout: 60_000 });
  await expectAnswerInChat(page, 'William Shakespeare.');
  expect(second).not.toBe(first);
  // A further task of the workgroup starts at once on the workgroup's running VM (the hub's `session.start` hint).
  const [secondSession] = await mainSessions(orchestrator, second);
  expect(secondSession!.seconds).toBeLessThan(10);

  // The follow-up from the first task's chat goes to that task and its (idle) main session; no task is created.
  await home(page);
  await page.getByTestId('home.all-tasks').click();
  await page.getByTestId('tasks.row').filter({ hasText: 'What is the capital of France?' }).click();
  await expect(page).toHaveURL(new RegExp(`/t/${first}$`));
  await page.getByTestId('chat.composer.prompt').fill('And of Italy?');
  await page.getByTestId('chat.composer.send').click();
  await expectAnswerInChat(page, 'Rome.');
  await expect(page.getByTestId('chat.output')).toHaveText([/Paris\.$/, /Rome\.$/]);
  await expect(page.getByTestId('chat.prompt.text')).toHaveText(['What is the capital of France?', 'And of Italy?']);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok).toBe(true);
  expect(await texts(first, 'prompt', 'text')).toEqual(['What is the capital of France?', 'And of Italy?']);
  expect(await texts(second, 'prompt', 'text')).toEqual(['Who wrote Hamlet?\n\nOne line is enough.']);
  expect(await mainSessions(orchestrator, first)).toHaveLength(1);
  expect(await outputs(second)).toEqual(['William Shakespeare.']);

  // Titles derived from the first line; both tasks in the org's default workgroup, nothing else created.
  expect(await texts(first, 'task.meta', 'title')).toEqual(['What is the capital of France?']);
  expect(await texts(second, 'task.meta', 'title')).toEqual(['Who wrote Hamlet?']);
  const tasks = await orchestrator.hubQuery<{ id: string; wg_id: string; in_default: boolean }>(
    `SELECT t.id, t.wg_id, t.wg_id = o.default_wg AS in_default FROM hub.task t JOIN hub.org o ON o.id = t.org_id
      WHERE t.org_id = '${uuid(onboarded.org)}' ORDER BY t.created_at`,
  );
  expect(tasks.map((task) => [task.id, task.in_default])).toEqual([
    [first, true],
    [second, true],
  ]);

  // The task list shows both tasks by title.
  await home(page);
  await page.getByTestId('home.all-tasks').click();
  await expect(page).toHaveURL(new RegExp(`/o/${onboarded.org}/w/${tasks[0]!.wg_id}$`));
  await expect(page.getByTestId('tasks.row.title')).toHaveText(['Who wrote Hamlet?', 'What is the capital of France?']);

  // No project object: not in the schema, not in the task API, not on the screens.
  expect(
    await orchestrator.hubQuery(
      `SELECT table_name FROM information_schema.columns WHERE table_schema = 'hub'
         AND (table_name LIKE '%project%' OR column_name LIKE '%project%')`,
    ),
  ).toEqual([]);
  const user = await PageUser.open(page, onboarded.recoveryCode);
  for (const task of [first, second]) {
    expect(JSON.stringify(await user.request('GET', `/v1/tasks/${task}`))).not.toMatch(/project/i);
  }
  await expect(page.getByText(/project/i)).toHaveCount(0);
  await page.getByTestId('tasks.row').filter({ hasText: 'What is the capital of France?' }).click();
  await expect(page).toHaveURL(new RegExp(`/t/${first}$`));
  await expect(page.getByTestId('task.title')).toHaveText('What is the capital of France?');
  await expect(page.getByText(/project/i)).toHaveCount(0);
});
