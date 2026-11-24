// E2E-36 (07-testing.md section 2.1; 05-runner.md section 10.1; ADR-010): own hardware uses own credentials. A further
// runner instance enrolls as an org device of the user's private org (device invitation from the devices screen,
// assigned to the default workgroup; it has no platform OpenRouter key - the runner refuses one on org devices). The
// first prompt without any credential of the user ends with `model_access_unavailable` on that device and never
// reaches the model; after the user stored an API key, the next prompt runs on the same device with the key, and
// the session environment holds no credential. T1 only.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  expectNoCredentialLeak,
  expectOnlySessionToken,
  failureReason,
  loadModelScenario,
  mainUsage,
  storeApiKey,
  testApiKey,
} from '../fixtures/model.ts';
import { expectAnswerInChat, home, mainSessions, onboard, sendPrompt, uuid } from '../fixtures/platform.ts';
import { navTo } from '../fixtures/nav.ts';

const OWN = JSON.parse(readFileSync(new URL('../scenarios/e2e-36-own-credentials.json', import.meta.url), 'utf8')) as {
  name: string;
};
/** A further runner instance of the run (`routes/runner_instances.py`), removed at the end. */
const ORG_RUNNER = 'runner-6';

test.describe.configure({ mode: 'serial' });

// E2E-36 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-36 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-36 @R-3.5 @R-4.5 @R-P.5 @needs-vm an org device without the user’s credentials refuses with model_access_unavailable and works with the user’s API key', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const apiKey = testApiKey();
  await orchestrator.call('POST', `/runners/${ORG_RUNNER}/spawn`, { device: 'org', pool_size: 1 });
  try {
    await authenticator('platform');
    await onboard(page, orchestrator, 'Ada E2E36');

    // The org device: invitation for the default workgroup in the devices screen, enrollment on the runner.
    await navTo(page, 'devices');
    await page.getByTestId('devices.add.open').click();
    await page.getByTestId('devices.add.submit').click();
    const code = (await page.getByTestId('devices.add.code').textContent())!.trim();
    const enrolled = await orchestrator.enroll(ORG_RUNNER, code, false);
    expect(enrolled.rc, enrolled.stderr).toBe(0);
    const device = uuid(/device_id=([0-9a-f-]{36})/.exec(enrolled.stdout)![1]!);
    await expect(page.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'true', { timeout: 60_000 });
    await page.getByTestId('devices.add.done').click();
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ kind: string; state: string; online: boolean }>(
              `SELECT d.kind, d.state, p.device_id IS NOT NULL AS online FROM hub.device d
                 LEFT JOIN hub.device_presence p ON p.device_id = d.id WHERE d.id = '${device}'`,
            )
          )[0],
        { timeout: 60_000 },
      )
      .toEqual({ kind: 'org', state: 'active', online: true });
    // The org device's warm pool, as the runner and as the hub's capacity report see it (the run's platform runner
    // may not be enrolled when this spec runs alone).
    await expect
      .poll(async () => (await orchestrator.pool())[ORG_RUNNER]?.ready ?? 0, { timeout: 240_000, intervals: [1000] })
      .toBeGreaterThanOrEqual(1);
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ ready: number | null }>(
              `SELECT (capacity->>'pool_ready')::int AS ready FROM hub.device WHERE id = '${device}'`,
            )
          )[0]?.ready ?? 0,
        { timeout: 120_000, intervals: [500] },
      )
      .toBeGreaterThanOrEqual(1);
    await home(page);

    // Without own credentials: refused on the org device, the model double sees nothing.
    await loadModelScenario(orchestrator, OWN, { kind: 'api_key', token: apiKey });
    const refusedTask = await sendPrompt(page, 'E2E36-NOKEY: who pays for this?');
    await expect
      .poll(() => failureReason(orchestrator, refusedTask), { timeout: 120_000 })
      .toBe('model_access_unavailable');
    expect(await mainSessions(orchestrator, refusedTask)).toEqual([expect.objectContaining({ device_id: device })]);
    await expect(page.getByTestId('countdown.failed')).toHaveAttribute('data-reason', 'model_access_unavailable', {
      timeout: 30_000,
    });
    expect((await orchestrator.doubleState('model')).requests).toBe(0);

    // With the user's API key (preference still automatic): the same org device answers with the key.
    await storeApiKey(page, apiKey);
    await home(page);
    const task = await sendPrompt(page, 'E2E36-APIKEY: and with my key?');
    await expectAnswerInChat(page, 'Own key answer kz36-own-s6r2y4.');
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const [session] = await mainSessions(orchestrator, task);
    expect(session).toMatchObject({ device_id: device });
    await expect
      .poll(async () => (await mainUsage(orchestrator, task))?.model_access ?? null, { timeout: 30_000 })
      .toBe('api_key');
    expect((await mainUsage(orchestrator, task))?.cost_basis).toBe('list_price');
    const env = await expectOnlySessionToken(orchestrator, session!.id, [apiKey]);
    expect(env.model.ANTHROPIC_MODEL).toBeUndefined();

    await expectNoCredentialLeak(orchestrator, [apiKey]);
  } finally {
    await orchestrator.call('POST', `/runners/${ORG_RUNNER}/remove`).catch(() => undefined);
  }
});
