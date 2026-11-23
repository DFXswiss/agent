// E2E-06 (07-testing.md section 2.1, 03-api.md section 4.6.1): the first task of a new private org on a warm pool.
// The runner is enrolled as a platform device by the operator (`invite-device`, `agent-runner enroll --platform`,
// `certify-pending`); the browser assigns it automatically on the first send. Asserted: countdown shown, session
// transitions in order, the answer reaches the user, `first_working_at − created_at < 10 s` from the hub database,
// `agent_kind = claude-code`. Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  expectAnswerInChat,
  expectStartOrder,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  transitions,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-06-first-task.json', import.meta.url), 'utf8'),
);

test.describe.configure({ mode: 'serial' });

test('@E2E-06 @R-0.3 @R-0.4 @R-2.1 @R-2.5 @R-3.1 @R-3.10 @R-3.11 @R-4.1 @R-4.8 @R-P.5 @needs-vm first task of a new private org works in under 10 s on a warm pool', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(420_000);
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);

  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator);
  await expect(page.locator('[data-testid^="composer.chip."]')).toHaveCount(4);
  // A suggestion fills the composer with its starting text and focuses it; the user completes or replaces it.
  await page.getByTestId('composer.chip.research').click();
  await expect(page.getByTestId('composer.prompt')).toHaveValue('Summarize the current state of ');
  await expect(page.getByTestId('composer.prompt')).toBeFocused();
  await page.getByTestId('composer.chip.plan').click();
  await expect(page.getByTestId('composer.prompt')).toHaveValue('Write a short plan for ');
  const task = await sendPrompt(page, 'What is 2+2?');

  // Countdown with digits, then the one-line summary once the session works.
  await expect(page.getByTestId('countdown.panel').first()).toBeVisible();
  await expect(page.getByTestId('countdown.summary')).toHaveText(/^Ready in \d+\.\d s$/, { timeout: 60_000 });

  const [session] = await mainSessions(orchestrator, task);
  expect(session).toMatchObject({ device_id: device, agent_kind: 'claude-code' });
  expect(session!.seconds).not.toBeNull();
  expect(session!.seconds!).toBeLessThan(10);
  expectStartOrder(await transitions(orchestrator, session!.id));

  // The answer: the model double's turn was consumed; prompt and answer show in the chat (decrypted in the browser,
  // the hub stores only ciphertext), and the user's own keys open the stored output.
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await expect(page.getByTestId('chat.prompt.text')).toHaveText('What is 2+2?');
  await expectAnswerInChat(page, '2+2 is 4.');
  expect((await taskBodies(page, orchestrator, onboarded, task, 'session.output')).map((b) => b.text)).toContain(
    '2+2 is 4.',
  );
});
