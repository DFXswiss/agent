// LIVE-04 (07-testing.md sections 2.4 and 6; LVT-01): a user's own Anthropic API key, optional. Runs only when the
// operator provides a key (`E2E_LIVE_ANTHROPIC_KEY`, handed over by `live/driver.py` from `AGENT_LIVE_ANTHROPIC_API_KEY`
// or a keychain item); without one the test is skipped with the reason "no key" and the skip is written to
// `<iteration>/live/live-04.json` (no failure). With a key: the key is deposited sealed in Node under the test user's
// credential key (the page never holds it), the user chooses access `api_key` with a Haiku-class model, and one mini
// turn runs through the gateway and the live bridge to the provider. Asserted: the answer, the session's access
// `api_key` with the chosen model and the list-price cost basis.
import { expect, test } from '../fixtures/test.ts';
import { chooseModelAccess, expectNoCredentialLeak, mainUsage } from '../fixtures/model.ts';
import { home, onboard, platformDevice, poolReady, sendPrompt } from '../fixtures/platform.ts';
import { depositCredential, writeResult } from './helpers.ts';

const MODEL = 'claude-haiku-5-5';
const PROMPT = 'LIVE04: Reply with the single word pong.';

test.describe.configure({ mode: 'serial' });

test('@LIVE-04 @R-4.3 @R-P.6 @live @needs-vm own Anthropic API key: one mini turn through the gateway (optional)', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(600_000);
  const key = process.env.E2E_LIVE_ANTHROPIC_KEY;
  const results: Record<string, unknown> = { test: 'LIVE-04', model: MODEL };
  if (!key) {
    writeResult(e2e.iterationDir, 'live-04', { ...results, skipped: 'no key' });
    test.skip(true, 'skipped - no Anthropic API key was provided (optional live test)');
    return;
  }
  await orchestrator.call('POST', '/live/upstreams', { platform_key: null });
  try {
    await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, 'Ada Live04');
    await depositCredential(page, onboarded.recoveryCode, 'anthropic_api_key', { key });
    await chooseModelAccess(page, 'api_key', MODEL);
    await expect(page.getByTestId('credentials.item.anthropic_api_key')).toBeVisible();

    await home(page);
    const task = await sendPrompt(page, PROMPT);
    await expect(page.getByTestId('chat.output').filter({ hasText: /pong/i })).toBeVisible({ timeout: 180_000 });
    await expect
      .poll(async () => (await mainUsage(orchestrator, task))?.requests ?? 0, { timeout: 60_000 })
      .toBeGreaterThanOrEqual(1);
    const usage = (await mainUsage(orchestrator, task))!;
    results.usage = usage;
    expect(usage).toMatchObject({ model_access: 'api_key', model_id: MODEL, cost_basis: 'list_price' });
    expect(usage.input_tokens).toBeGreaterThan(0);
    await expectNoCredentialLeak(orchestrator, [key]);
  } finally {
    writeResult(e2e.iterationDir, 'live-04', results);
  }
});
