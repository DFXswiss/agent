// E2E-16 (07-testing.md section 2.1; 05-runner.md sections 10.4, 10.6; 06-web.md sections 5.2, 8.2; ADR-010, ADR-025):
// usage, cost, context and quotas. Three tasks of one private org on the platform device, each answered by the model
// double with fixed token counts: (1) the platform access with OpenRouter-shaped answers (`usage.cost`, provider),
// (2) the user's API key (cost at the catalog list price), (3) the user's Claude subscription (list price as
// subscription equivalent). For each, the hub's counters hold the tokens of every answered request, the cost with
// its basis and the context of the latest request, and the task view's usage strip shows them. After the
// subscription session the runner polls the owner's quota at the usage double (subscription credential and the
// oauth beta only); the settings show the quota bars to the owner. No credential value leaks (M2c scan). T1 only.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  CLAUDE_MODEL,
  PLATFORM_MODEL,
  PLATFORM_REPLY_MICRO_USD,
  answeredModels,
  anySubscriptionQuota,
  chooseModelAccess,
  connectSubscription,
  expectNoCredentialLeak,
  expectUsage,
  listPriceMicroUsd,
  loadModelScenario,
  mainUsage,
  openSettings,
  randomText,
  storeApiKey,
  testApiKey,
  usageScenario,
} from '../fixtures/model.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const scenario = (name: string): { name: string } =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8')) as { name: string };
const PLATFORM = scenario('e2e-16-usage-platform');
const API_KEY = scenario('e2e-16-usage-api-key');
const SUBSCRIPTION = scenario('e2e-16-usage-subscription');
const RESETS = { five_hour: '2026-11-30T15:00:00Z', seven_day: '2026-12-04T09:00:00Z' };
const USAGE_DOCUMENT = {
  five_hour: { utilization: 37.5, resets_at: RESETS.five_hour },
  seven_day: { utilization: 12, resets_at: RESETS.seven_day },
  seven_day_opus: null,
};

test.describe.configure({ mode: 'serial' });

// E2E-16 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-16 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-16 @R-6.4 @R-6.5 @R-14.4 @R-P.5 @needs-vm counters, cost basis and context per access kind in the task view, quota bars for the owner', async ({
  page,
  browser,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const seed = `e2e16-${randomText(6)}`;
  const apiKey = testApiKey();
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada E2E16');

  // (1) Platform access (the default of a user without credentials on a platform device): OpenRouter's usage.cost.
  await loadModelScenario(orchestrator, PLATFORM, { kind: 'platform' });
  const platformTask = await sendPrompt(page, 'E2E16-PLATFORM: how much does this cost?');
  await expectAnswerInChat(page, 'Platform answer kz16-platform-r4t8w2.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const platformModels = await answeredModels(orchestrator, PLATFORM.name);
  expect(platformModels.length).toBeGreaterThanOrEqual(1);
  expect(new Set(platformModels)).toEqual(new Set([PLATFORM_MODEL]));
  const platformUsage = await expectUsage(page, orchestrator, platformTask, {
    basis: 'provider',
    requests: platformModels.length,
    microUsd: PLATFORM_REPLY_MICRO_USD * platformModels.length,
    model: PLATFORM_MODEL,
  });
  expect(platformUsage).toMatchObject({
    model_access: 'platform',
    model_id: PLATFORM_MODEL,
    last_provider: 'DoubleCloud',
  });

  // (2) The user's API key, model chosen in the settings: list price of the catalog.
  await storeApiKey(page, apiKey);
  await chooseModelAccess(page, 'api_key', CLAUDE_MODEL);
  await loadModelScenario(orchestrator, API_KEY, { kind: 'api_key', token: apiKey });
  await home(page);
  const apiKeyTask = await sendPrompt(page, 'E2E16-APIKEY: and with my own key?');
  await expectAnswerInChat(page, 'API key answer kz16-apikey-h3m9q5.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const apiKeyModels = await answeredModels(orchestrator, API_KEY.name);
  expect(apiKeyModels).toContain(CLAUDE_MODEL);
  const apiKeyUsage = await expectUsage(page, orchestrator, apiKeyTask, {
    basis: 'list_price',
    requests: apiKeyModels.length,
    microUsd: listPriceMicroUsd(apiKeyModels),
    model: CLAUDE_MODEL,
  });
  expect(apiKeyUsage).toMatchObject({ model_access: 'api_key', model_id: CLAUDE_MODEL, last_provider: null });

  // (3) The user's Claude subscription (relay login with the login doubles): list price as subscription equivalent.
  await connectSubscription(page, browser, orchestrator, seed);
  await usageScenario(orchestrator, {
    name: 'e2e-16-quota',
    document: USAGE_DOCUMENT,
    login_seed: seed,
    min_requests: 1,
  });
  await chooseModelAccess(page, 'subscription', CLAUDE_MODEL);
  await loadModelScenario(orchestrator, SUBSCRIPTION, { kind: 'subscription', login_seed: seed });
  await home(page);
  const subscriptionTask = await sendPrompt(page, 'E2E16-SUBSCRIPTION: and with my subscription?');
  await expectAnswerInChat(page, 'Subscription answer kz16-sub-p7c2x6.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const subscriptionModels = await answeredModels(orchestrator, SUBSCRIPTION.name);
  expect(subscriptionModels).toContain(CLAUDE_MODEL);
  const subscriptionUsage = await expectUsage(page, orchestrator, subscriptionTask, {
    basis: 'subscription_equivalent',
    requests: subscriptionModels.length,
    microUsd: listPriceMicroUsd(subscriptionModels),
    model: CLAUDE_MODEL,
  });
  expect(subscriptionUsage).toMatchObject({ model_access: 'subscription', model_id: CLAUDE_MODEL });

  // Quotas: the runner polls the owner's subscription at the usage double (next 30 s tick after the session); the
  // hub keeps the snapshot of the owner and the settings show the bars (only the user's own rows).
  await expect.poll(async () => (await orchestrator.doubleState('usage')).ok, { timeout: 120_000 }).toBe(true);
  const snapshots = await orchestrator.hubQuery<{ window: string; used_percent: number }>(
    `SELECT "window", used_percent::float8 AS used_percent FROM hub.quota_snapshot
      WHERE owner_id = (SELECT owner_id FROM hub.session WHERE task_id = '${uuid(subscriptionTask)}' AND depth = 0)
      ORDER BY "window"`,
  );
  expect(snapshots).toEqual([
    { window: 'five_hour', used_percent: 37.5 },
    { window: 'seven_day', used_percent: 12 },
  ]);
  await openSettings(page);
  await expect
    .poll(
      async () => {
        await page.getByTestId('quotas.reload').click();
        return page
          .getByTestId('quotas.bar.five_hour.used')
          .textContent({ timeout: 2_000 })
          .catch(() => null);
      },
      { timeout: 60_000 },
    )
    .toBe('37.5%');
  await expect(page.getByTestId('quotas.bar.seven_day.used')).toHaveText('12%');
  await expect(page.getByTestId('quotas.bar.five_hour.reset')).toContainText('Resets');
  await expect(page.getByTestId('quotas.unavailable')).toHaveCount(0);
  // Later specs connect further subscriptions on this device: from now on the double answers any subscription token
  // (with the oauth beta) instead of only this owner's.
  await anySubscriptionQuota(orchestrator, USAGE_DOCUMENT);

  // Every task keeps its own numbers (the later sessions did not change the platform task's counters).
  expect(await mainUsage(orchestrator, platformTask)).toEqual(platformUsage);

  await expectNoCredentialLeak(orchestrator, [apiKey], [seed]);
});
