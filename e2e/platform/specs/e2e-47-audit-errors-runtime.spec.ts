// E2E-47 (KOR-40; ADR-063, ADR-064): the audit records a refused write after its rollback, the session's own output is
// summarised (`event.append_batch`), a real error of the guest reaches the task history (`session.error`), and the web
// shows the runtime of task and session and whether the hub reaches the session's device. Hermetic, T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-47-audit-errors-runtime.json', import.meta.url), 'utf8'),
);
// the kinds the audit job summarises (`agent_hub.audit.eventbatch.BATCHED_KINDS`)
const BATCHED = ['session.output', 'session.terminal', 'session.tool', 'session.hook', 'session.file'];
const FIRST = 'E2E46-FIRST kz46-prompt-a4c9e2v7: answer briefly';
const SECOND = 'E2E46-SECOND kz46-prompt-f1h5k8n3: answer again';

test.describe.configure({ mode: 'serial' });

test('@E2E-47 @R-6.3 @R-6.7 @R-7.2 @needs-vm refused writes, the session output summary, guest errors, runtime and reachability', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz46-name-u2w7');
  const task = await sendPrompt(page, FIRST);
  await expectAnswerInChat(page, 'First answer kz46-answer-r3t8w1q5.');
  const [session] = await mainSessions(orchestrator, task);

  // Runtime of the task and the session, and the session's device reachable (concept section 7).
  await expect(page.getByTestId('task.runtime')).toBeVisible();
  await page.getByTestId('task.tabs.sessions').click();
  const node = page.getByTestId('sessions.node').first();
  await expect(node.getByTestId('session.runtime')).toBeVisible();
  await expect(node.getByTestId('session.device.reachable')).toHaveAttribute('data-reachable', 'true', {
    timeout: 60_000,
  });
  await page.getByTestId('task.tabs.chat').click();

  // A refused write leaves its row although the route's transaction was rolled back.
  const user = await PageUser.open(page, onboarded.recoveryCode);
  await expect(
    user.request('PUT', '/v1/me/settings', { model_access: 'nope', model_id: null, locale: 'de' }),
  ).rejects.toThrow(/400/);
  await expect
    .poll(async () =>
      orchestrator.hubQuery<{ result: string; route: string }>(
        `SELECT result, detail->>'route' AS route FROM hub.audit WHERE action = 'request.refused'
           AND detail->>'route' = '/v1/me/settings' ORDER BY seq`,
      ),
    )
    .toEqual([{ result: 'rejected', route: '/v1/me/settings' }]);

  // The guest loses the session's credential: its next model request reports it, the runner provisions again.
  await orchestrator.call('POST', `/sessions/${uuid(session!.id)}/guest-api-restart`, {});
  await page.getByTestId('chat.composer.prompt').fill(SECOND);
  await page.getByTestId('chat.composer.send').click();
  await expectAnswerInChat(page, 'Second answer kz46-answer-y6p2m9d4.');
  await expect(page.locator('[data-testid="chat.guest-error"][data-code="credential_missing"]').first()).toBeVisible({
    timeout: 60_000,
  });

  // The session's own output is summarised per session and scope by the audit job, not one row per event.
  await expect
    .poll(async () => (await auditRows(orchestrator, task, 'event.append_batch')).length, { timeout: 120_000 })
    .toBeGreaterThan(0);
  const single = await auditRows(orchestrator, task, 'event.append');
  expect(single.filter((row) => BATCHED.includes(String(row.detail.kind)))).toEqual([]);
  // the guest's error is no bulk record: it keeps a row of its own
  expect(single.filter((row) => row.detail.kind === 'session.error').length).toBeGreaterThan(0);
});
