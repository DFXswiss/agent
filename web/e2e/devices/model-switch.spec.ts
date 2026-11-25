// A switch of the model access (ADR-081 section 5, KOR-50) against the real hub and the device double: the device's
// outbox audit row `session.model_switched` (scripts/hub_test_metrics.py) is stored by the hub, which notifies the
// task's owner (`model.switched`); the task page shows the newest switch as a line ("Model switched: … · used up until
// HH:MM", the operator's subscription named as such, without a model, without a release) and the notification center
// names both accesses and the release.
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { seedAudit } from '../helpers/monitoring.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

const UNTIL = new Date(Date.UTC(2030, 0, 2, 13, 5)).toISOString();
const CLOCK = /\d{2}:\d{2}/;

test('a model switch shows in the task and in the notifications', async ({ page, baseURL }) => {
  test.setTimeout(120_000);
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Switch the model');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect.poll(async () => (await mainSession(task)).state, { timeout: 20_000 }).toBe('working');
  const line = page.getByTestId('task.model-switch');
  await expect(line).toHaveCount(0);
  // used up until a time, with the models: the line follows the session's next state
  await seedAudit(user.device, main.session_id, 'session.model_switched', {
    from_access: 'subscription',
    to_access: 'platform',
    from_model: 'claude-sonnet-4-5',
    to_model: 'deepseek/deepseek-v4.1-flash-1017',
    cause: 'exhausted',
    until: UNTIL,
  });
  await walk(main.session_id, ['idle']);
  await expect(line).toContainText(
    'Model switched: Claude subscription (claude-sonnet-4-5) → DFX access over OpenRouter (deepseek/deepseek-v4.1-flash-1017) · used up until',
  );
  await expect(line).toContainText(CLOCK);
  // released again, from the operator's subscription, without models
  await seedAudit(user.device, main.session_id, 'session.model_switched', {
    from_access: 'operator',
    to_access: 'subscription',
    from_model: null,
    to_model: null,
    cause: 'released',
    until: null,
  });
  await walk(main.session_id, ['working']);
  await expect(line).toHaveText(
    'Model switched: operator subscription → Claude subscription · subscription free again',
  );
  // used up without a known release
  await seedAudit(user.device, main.session_id, 'session.model_switched', {
    from_access: 'operator',
    to_access: 'platform',
    from_model: null,
    to_model: null,
    cause: 'exhausted',
    until: null,
  });
  await walk(main.session_id, ['idle']);
  await expect(line).toHaveText('Model switched: operator subscription → DFX access over OpenRouter · used up');
  // the owner was told each time
  await page.getByTestId('bell.link').click();
  const texts = page.locator('[data-testid="notifications.item"][data-kind="model.switched"]').getByTestId('notifications.text');
  await expect(texts).toHaveCount(3);
  await expect(texts.filter({ hasText: 'because' })).toContainText(
    'model switched: Claude subscription → DFX access over OpenRouter because Claude subscription is used up until',
  );
  await expect(texts.filter({ hasText: 'operator subscription → Claude subscription' })).toHaveCount(1);
  await expect(texts.filter({ hasText: 'operator subscription → DFX access over OpenRouter' })).toHaveCount(1);
});
