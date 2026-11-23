// E2E-27 (07-testing.md section 2.1; 05-runner.md sections 10.1-10.3; 03-api.md section 2; ADR-010): the model access
// variants against the model double, which accepts per step only the credential header the gateway must inject for
// the access kind (`orchestrator/routes/model.py`): (1) the platform default - the DeepSeek model ID with the run's
// platform OpenRouter key as Bearer token; (2) the user's Anthropic API key as `x-api-key` (no `authorization`);
// (3) the user's Claude subscription token as Bearer with `oauth-2025-04-20` in `anthropic-beta`. Model IDs as
// configured (catalog default for the platform, the settings' choice otherwise). For each session the Claude Code
// environment holds only the session token (in `ANTHROPIC_AUTH_TOKEN` and `PLATFORM_SESSION_TOKEN`), an empty
// `ANTHROPIC_API_KEY` and no credential value. Budget step: with `PLATFORM_BUDGET_USD_PER_ORG_DAY` below what the org
// spent today, a further platform session ends with `budget_exceeded` and never reaches the model. T1 only.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  CLAUDE_MODEL,
  PLATFORM_MODEL,
  anySubscriptionQuota,
  chooseModelAccess,
  connectSubscription,
  expectNoCredentialLeak,
  expectOnlySessionToken,
  failureReason,
  loadModelScenario,
  mainUsage,
  randomText,
  setBudget,
  storeApiKey,
  testApiKey,
} from '../fixtures/model.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const scenario = (name: string): { name: string } =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8')) as { name: string };
const PLATFORM = scenario('e2e-27-access-platform');
const API_KEY = scenario('e2e-27-access-api-key');
const SUBSCRIPTION = scenario('e2e-27-access-subscription');
const HAIKU = 'claude-haiku-5-5';
/** Below the platform spend of step (1): at least one answered request at 0.0021 USD. */
const LOW_BUDGET_USD = '0.001';

test.describe.configure({ mode: 'serial' });

// E2E-27 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-27 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-27 @R-4.2 @R-4.3 @R-4.5 @R-4.6 @R-4.7 @R-P.13 @R-P.5 @needs-vm the gateway injects the credential of each access kind, the session holds only its token, a low platform budget ends with budget_exceeded', async ({
  page,
  browser,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const seed = `e2e27-${randomText(6)}`;
  const apiKey = testApiKey();
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada E2E27');

  // The subscription session of step 3 makes the runner poll its owner's quota: answered inside the run.
  await anySubscriptionQuota(orchestrator);
  try {
    // (1) Platform default: no credential, preference automatic, platform device → DeepSeek through the platform key.
    await loadModelScenario(orchestrator, PLATFORM, { kind: 'platform' });
    const platformTask = await sendPrompt(page, 'E2E27-PLATFORM: which model answers?');
    await expectAnswerInChat(page, 'Platform default answer kz27-platform-b5n1v8.');
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const [platformSession] = await mainSessions(orchestrator, platformTask);
    expect(platformSession).toMatchObject({ device_id: device });
    await expect
      .poll(async () => (await mainUsage(orchestrator, platformTask))?.cost_micro_usd ?? 0)
      .toBeGreaterThan(0);
    expect(await mainUsage(orchestrator, platformTask)).toMatchObject({
      model_access: 'platform',
      model_id: PLATFORM_MODEL,
      cost_basis: 'provider',
    });
    const platformEnv = await expectOnlySessionToken(orchestrator, platformSession!.id);
    expect(platformEnv.credentials_checked).toBe(1); // the platform key
    for (const name of [
      'ANTHROPIC_MODEL',
      'ANTHROPIC_DEFAULT_OPUS_MODEL',
      'ANTHROPIC_DEFAULT_SONNET_MODEL',
      'ANTHROPIC_DEFAULT_HAIKU_MODEL',
      'CLAUDE_CODE_SUBAGENT_MODEL',
    ])
      expect(platformEnv.model[name], name).toBe(PLATFORM_MODEL);

    // (2) API key with the model chosen in the settings.
    await storeApiKey(page, apiKey);
    await chooseModelAccess(page, 'api_key', CLAUDE_MODEL);
    await loadModelScenario(orchestrator, API_KEY, { kind: 'api_key', token: apiKey });
    await home(page);
    const apiKeyTask = await sendPrompt(page, 'E2E27-APIKEY: and now?');
    await expectAnswerInChat(page, 'API key answer kz27-apikey-d2f6k3.');
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const [apiKeySession] = await mainSessions(orchestrator, apiKeyTask);
    await expect
      .poll(async () => (await mainUsage(orchestrator, apiKeyTask))?.model_access ?? null, { timeout: 30_000 })
      .toBe('api_key');
    expect(await mainUsage(orchestrator, apiKeyTask)).toMatchObject({
      model_id: CLAUDE_MODEL,
      cost_basis: 'list_price',
    });
    const apiKeyEnv = await expectOnlySessionToken(orchestrator, apiKeySession!.id, [apiKey]);
    expect(apiKeyEnv.model.ANTHROPIC_MODEL).toBeUndefined(); // no platform model slots for own credentials

    // (3) Subscription token (relay login with the login doubles), another catalog model.
    await connectSubscription(page, browser, orchestrator, seed);
    await chooseModelAccess(page, 'subscription', HAIKU);
    await loadModelScenario(orchestrator, SUBSCRIPTION, { kind: 'subscription', login_seed: seed });
    await home(page);
    const subscriptionTask = await sendPrompt(page, 'E2E27-SUBSCRIPTION: and with the subscription?');
    await expectAnswerInChat(page, 'Subscription answer kz27-sub-w9g4j7.');
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const [subscriptionSession] = await mainSessions(orchestrator, subscriptionTask);
    await expect
      .poll(async () => (await mainUsage(orchestrator, subscriptionTask))?.model_access ?? null, { timeout: 30_000 })
      .toBe('subscription');
    expect(await mainUsage(orchestrator, subscriptionTask)).toMatchObject({
      model_id: HAIKU,
      cost_basis: 'subscription_equivalent',
    });
    await expectOnlySessionToken(orchestrator, subscriptionSession!.id, [apiKey], [seed]);

    // Budget: the org spent more than the new daily limit on the platform today; a further platform session is
    // refused before it reaches the model (the double's script is complete and stays untouched).
    await setBudget(orchestrator, LOW_BUDGET_USD);
    await chooseModelAccess(page, 'platform');
    await home(page);
    const budgetTask = await sendPrompt(page, 'E2E27-BUDGET: one more on the platform');
    await expect.poll(() => failureReason(orchestrator, budgetTask), { timeout: 120_000 }).toBe('budget_exceeded');
    await expect(page.getByTestId('countdown.failed')).toHaveAttribute('data-reason', 'budget_exceeded', {
      timeout: 30_000,
    });
    const audit = await orchestrator.hubQuery<{ action: string; detail: { reason?: string } }>(
      `SELECT action, detail FROM hub.audit WHERE task_id = '${uuid(budgetTask)}' AND action = 'session.failed'`,
    );
    expect(audit.map((row) => row.detail.reason)).toEqual(['budget_exceeded']);
    const state = await orchestrator.doubleState('model');
    expect(state).toMatchObject({ ok: true, failures: [] });

    await expectNoCredentialLeak(orchestrator, [apiKey], [seed]);
  } finally {
    await setBudget(orchestrator, null);
  }
});
