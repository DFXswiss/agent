// LIVE-03 (07-testing.md sections 2.4 and 6; R-P.10; LVT-01, built on the SCN-05 smoke; PRB-01 ran the first check):
// a Claude subscription, read only. The operator's driver (`live/driver.py` with the agent repository's probe) reads
// only the current access token of a profile the operator chose (never an excluded profile, never refresh, login or
// logout; profile files and keychain metadata hashed before and after the run - any change fails the driver) and hands
// it to the run in `E2E_LIVE_SUBSCRIPTION_TOKEN` (scanned for in every artefact). The spec deposits it like a stored
// setup token - sealed in Node under the test user's credential key, so the page never holds it - and runs one
// Haiku-class mini turn through the gateway and the live bridge to the provider. Asserted: the answer, the session's
// access `subscription` with the chosen model, the cost basis `subscription_equivalent`. Then the runner polls the
// usage endpoint with the token: quotas shown, or - when the endpoint refuses the token - "not available" (documented
// in `<iteration>/live/live-03.json`, no failure).
import { expect, test } from '../fixtures/test.ts';
import { chooseModelAccess, expectNoCredentialLeak, mainUsage, openSettings } from '../fixtures/model.ts';
import { home, onboard, platformDevice, poolReady, sendPrompt, uuid } from '../fixtures/platform.ts';
import { depositCredential, required, writeResult } from './helpers.ts';

const MODEL = 'claude-haiku-5-5';
const PROMPT = 'LIVE03: Reply with the single word pong.';
const QUOTA_WAIT_MS = 120_000;

test.describe.configure({ mode: 'serial' });

test('@LIVE-03 @R-4.2 @R-6.5 @R-14.4 @R-P.6 @R-P.10 @live @needs-vm subscription access token read only: one mini turn through the gateway, quotas or not available', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(600_000);
  const token = required('E2E_LIVE_SUBSCRIPTION_TOKEN');
  await orchestrator.call('POST', '/live/upstreams', { platform_key: null });
  const results: Record<string, unknown> = { test: 'LIVE-03', model: MODEL };
  try {
    await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, 'Ada Live03');
    const userId = await depositCredential(page, onboarded.recoveryCode, 'claude_subscription', {
      token,
      email: null,
      kind: 'setup_token',
    });
    await chooseModelAccess(page, 'subscription', MODEL);
    await expect(page.getByTestId('credentials.item.claude_subscription')).toBeVisible();

    await home(page);
    const task = await sendPrompt(page, PROMPT);
    await expect(page.getByTestId('chat.output').filter({ hasText: /pong/i })).toBeVisible({ timeout: 180_000 });
    await expect
      .poll(async () => (await mainUsage(orchestrator, task))?.requests ?? 0, { timeout: 60_000 })
      .toBeGreaterThanOrEqual(1);
    const usage = (await mainUsage(orchestrator, task))!;
    results.usage = usage;
    expect(usage).toMatchObject({
      model_access: 'subscription',
      model_id: MODEL,
      cost_basis: 'subscription_equivalent',
    });
    expect(usage.input_tokens).toBeGreaterThan(0);
    // The token never in plaintext on the runner device (its cache holds the sealed record), in a VM or at the hub.
    await expectNoCredentialLeak(orchestrator, [token]);

    // Quotas: polled on the runner's next tick after the session; a refusal of the token is not a failure.
    const owner = uuid(userId);
    let windows: { window: string; used_percent: number }[] = [];
    await expect
      .poll(
        async () =>
          (windows = await orchestrator.hubQuery<{ window: string; used_percent: number }>(
            `SELECT "window", used_percent::float8 AS used_percent FROM hub.quota_snapshot WHERE owner_id = '${owner}'`,
          )).length,
        { timeout: QUOTA_WAIT_MS },
      )
      .toBeGreaterThan(0)
      .catch(() => undefined);
    const bridged = (await orchestrator.doubleState('usage')) as unknown as { bridged?: number };
    results.usage_endpoint = windows.length > 0 ? 'quotas available' : 'quotas not available';
    results.quota_windows = windows.map((row) => row.window).sort();
    results.bridged_requests = bridged.bridged ?? 0;
    await openSettings(page);
    if (windows.length > 0) await expect(page.getByTestId('quotas.bars')).toBeVisible();
    else await expect(page.getByTestId('quotas.unavailable')).toBeVisible();
  } finally {
    writeResult(e2e.iterationDir, 'live-03', results);
  }
});
