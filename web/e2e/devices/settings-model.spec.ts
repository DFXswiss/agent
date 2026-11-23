// Model access, credentials, quotas and the CK rotation in /settings (06-web.md sections 8.2 and 11 job 5, 03-api.md
// sections 4.1, 4.8 and 4.10, ADR-010, ADR-011) against the real hub and the device double: the preference and model
// from the hub's model catalog, the API key stored CK-encrypted and checked by the device, the Claude subscription by
// the relay (the authorization code as a follow-up request; the device stores the token) and by the mailbox automation
// with its fallback, failures and cancelling, the quota bars the polling device reports, deleting, and the rotation of
// CK when the device loses access. No token, key or password ever reaches the DOM; the CSP listener of the fixture
// fails on any violation.
import { devices, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { answer, reportQuotas, unassign } from '../helpers/credentials.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

const API_KEY = 'api-key-not-real';
const PASSWORD = 'not a real mailbox password';
const TOKEN = 'setup-token-not-real';

async function expectNoSecrets(page: Page): Promise<void> {
  const html = await page.content();
  for (const secret of [API_KEY, PASSWORD, TOKEN, 'gho_not_a_real_token']) expect(html).not.toContain(secret);
}

async function settings(page: Page): Promise<void> {
  await page.goto('/settings');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('credentials.section')).toBeVisible();
}

async function connectRelay(page: Page, device: string): Promise<void> {
  await page.getByTestId('claude.connect').click();
  await expect(page.getByTestId('claude.devices.select')).toHaveValue(device);
  await page.getByTestId('claude.start').click();
  await expect(page.getByTestId('claude.waiting')).toBeVisible();
}

test('saves the model access from the hub catalog @R-4.2', async ({ page, baseURL }) => {
  await freshUser(page, baseURL!);
  await settings(page);
  await expect(page.getByTestId('model.access.auto')).toBeChecked();
  await expect(page.getByTestId('model.platform-note')).toContainText('only on DFX execution devices');
  // automatic: no model is allowed for every access kind of the example catalog
  await expect(page.getByTestId('model.model.none')).toBeVisible();
  await page.getByTestId('model.access.platform').check();
  await expect(page.getByTestId('model.model').locator('option').first()).toHaveText(
    'Default (deepseek/deepseek-v4.1-flash-1017)',
  );
  await page.getByTestId('model.access.api_key').check();
  await page.getByTestId('model.model').selectOption('claude-sonnet-5-5');
  await page.getByTestId('model.save').click();
  await expect(page.getByTestId('model.saved')).toBeVisible();
  await settings(page);
  await expect(page.getByTestId('model.access.api_key')).toBeChecked();
  await expect(page.getByTestId('model.model')).toHaveValue('claude-sonnet-5-5');
});

test('stores, checks and deletes the API key encrypted @R-4.2 @R-5.1', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await settings(page);
  await expect(page.getByTestId('credentials.empty')).toBeVisible();
  await expect(page.getByTestId('api-key.input')).toHaveAttribute('type', 'password');
  await page.getByTestId('api-key.input').fill(API_KEY);
  await page.getByTestId('api-key.save').click();
  await expect(page.getByTestId('api-key.saved')).toBeVisible();
  await expect(page.getByTestId('api-key.input')).toHaveValue('');
  await expect(page.getByTestId('credentials.item.anthropic_api_key.version')).toContainText('Version 1');
  const record = await api.request('GET', '/v1/me/credentials/anthropic_api_key');
  expect(JSON.stringify(record)).not.toContain(API_KEY);
  // the device checks it (fresh org: the platform device was assigned automatically)
  await page.getByTestId('api-key.check').click();
  await page.getByTestId('api-key.check-flow.start').click();
  await expect(page.getByTestId('api-key.check-flow.waiting')).toBeVisible();
  const opened = await answer(user.device, [{ body: { step: 'done', valid: false }, final: true }]);
  expect(opened).toMatchObject({ kind: 'credential.check', body: { kind: 'anthropic_api_key' } });
  await expect(page.getByTestId('api-key.check-flow.result')).toHaveText(
    'The credential was refused. Store a new one.',
  );
  await page.getByTestId('api-key.check-flow.close').click();
  // replace it (version 2), then delete it
  await page.getByTestId('api-key.input').fill(`${API_KEY}-2`);
  await page.getByTestId('api-key.save').click();
  await expect(page.getByTestId('credentials.item.anthropic_api_key.version')).toContainText('Version 2');
  await expectNoSecrets(page);
  await page.getByTestId('credentials.item.anthropic_api_key.delete').click();
  await page.getByTestId('credentials.delete.cancel').click();
  await page.getByTestId('credentials.item.anthropic_api_key.delete').click();
  await page.getByTestId('credentials.delete.confirm').click();
  await expect(page.getByTestId('credentials.empty')).toBeVisible();
  expect(((await api.request('GET', '/v1/me/credentials')) as { credentials: unknown[] }).credentials).toEqual([]);
});

test('connects the subscription by the relay, shows the quotas and rotates CK when the device leaves @R-4.3 @R-6.4 @R-6.5', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await settings(page);
  await expect(page.getByTestId('quotas.unavailable')).toBeVisible();
  await connectRelay(page, user.device);
  expect(
    await answer(user.device, [{ body: { step: 'authorize', url: 'https://claude.ai/oauth/authorize?code=true' } }]),
  ).toMatchObject({ kind: 'claude.login', body: { mode: 'manual' } });
  await expect(page.getByTestId('claude.authorize.open')).toHaveAttribute(
    'href',
    'https://claude.ai/oauth/authorize?code=true',
  );
  await expect(page.getByTestId('claude.authorize.open')).toHaveAttribute('target', '_blank');
  await page.getByTestId('claude.code').fill('the-code#state');
  await page.getByTestId('claude.code.send').click();
  await expect(page.getByTestId('claude.code.send')).toHaveText('Code sent');
  const followUp = await answer(user.device, [
    {
      body: { step: 'done', email: 'claude@example.com' },
      final: true,
      store: { kind: 'claude_subscription', email: 'claude@example.com' },
    },
  ]);
  expect(followUp).toMatchObject({ kind: 'claude.login', body: { code: 'the-code#state' } });
  await expect(page.getByTestId('claude.connected')).toHaveText('The subscription claude@example.com is connected.');
  await expect(page.getByTestId('credentials.item.claude_subscription.detail')).toHaveText(
    'Account claude@example.com',
  );
  await expectNoSecrets(page);
  const stored = await api.request('GET', '/v1/me/credentials/claude_subscription');
  expect(JSON.stringify(stored)).not.toContain(TOKEN);
  // the device polls the quota of the owner of a subscription
  await reportQuotas(user.device, api.userId, [
    { window: 'five_hour', used_percent: 37.5, resets_in_s: 3600 },
    { window: 'seven_day', used_percent: 81, resets_in_s: null, severity: 'warning' },
    { window: 'weekly:claude-opus-5-5', used_percent: 12 },
  ]);
  await page.getByTestId('quotas.reload').click();
  await expect(page.getByTestId('quotas.bar.five_hour.used')).toHaveText('37.5%');
  await expect(page.getByTestId('quotas.bar.five_hour.reset')).toContainText('Resets');
  await expect(page.getByTestId('quotas.bar.seven_day.reset')).toHaveText('Reset time unknown');
  await expect(page.getByTestId('quotas.bar.weekly:claude-opus-5-5')).toContainText('Week, claude-opus-5-5');
  // the API key is the user's own record; the subscription token was stored by the platform device
  await page.getByTestId('api-key.input').fill(API_KEY);
  await page.getByTestId('api-key.save').click();
  await expect(page.getByTestId('api-key.saved')).toBeVisible();
  // the platform device leaves the default workgroup (the home screen listens to the org's live channel): the
  // browser renews CK, re-encrypts its own record and deletes the one the device stored (connect again)
  await page.goto(`/o/${user.org}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('composer.prompt')).toBeVisible();
  const me = (await api.request('GET', '/v1/me')) as { orgs: { org_id: string; default_wg: string }[] };
  await unassign(api, user.org, me.orgs[0]!.default_wg, user.device);
  // silently (ADR-058 section 4): no notice, the records show the result
  await expect
    .poll(
      async () =>
        ((await api.request('GET', '/v1/me/credentials')) as { credentials: { epoch: number }[] }).credentials,
    )
    .toEqual([expect.objectContaining({ kind: 'anthropic_api_key', epoch: 2, version: 2 })]);
  await expect(page.getByTestId('credential-rotation.item')).toHaveCount(0);
  await settings(page);
  await expect(page.getByTestId('credentials.item.anthropic_api_key')).toHaveAttribute('data-status', 'verified');
  await expect(page.getByTestId('credentials.item.claude_subscription')).toHaveCount(0);
});

test('runs the mailbox automation, falls back to the relay and reports failures and cancelling @R-4.3', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  await settings(page);
  await page.getByTestId('mailbox.imap_host').fill('imap.example.com');
  await page.getByTestId('mailbox.imap_user').fill('claude@example.com');
  await page.getByTestId('mailbox.imap_password').fill(PASSWORD);
  await page.getByTestId('mailbox.email').fill('claude@example.com');
  await page.getByTestId('mailbox.save').click();
  await expect(page.getByTestId('mailbox.saved')).toBeVisible();
  await expect(page.getByTestId('credentials.item.mailbox.detail')).toHaveText(
    'claude@example.com on imap.example.com',
  );
  await expect(page.getByTestId('mailbox.imap_password')).toHaveValue('');
  await expectNoSecrets(page);
  // check the mailbox: the device reports a failure
  await page.getByTestId('mailbox.check').click();
  await page.getByTestId('mailbox.check-flow.start').click();
  await expect(page.getByTestId('mailbox.check-flow.waiting')).toBeVisible();
  expect(
    await answer(user.device, [{ body: { step: 'failed', code: 'upstream_unreachable' }, final: true }]),
  ).toMatchObject({ kind: 'mailbox.check', body: {} });
  await expect(page.getByTestId('mailbox.check-flow.failed')).toContainText('The service could not be reached.');
  await page.getByTestId('mailbox.check-flow.close').click();
  // automatic login: progress, then the automation fails and the relay continues
  await page.getByTestId('claude.connect').click();
  await expect(page.getByTestId('claude.mode.automatic')).toBeChecked();
  await page.getByTestId('claude.start').click();
  await expect(page.getByTestId('claude.waiting')).toBeVisible();
  expect(await answer(user.device, [{ body: { step: 'progress', stage: 'email_entered' } }])).toMatchObject({
    body: { mode: 'automatic' },
  });
  await expect(page.getByTestId('claude.progress')).toHaveText('The device entered the e-mail address…');
  await answer(user.device, [{ body: { step: 'authorize', url: 'https://claude.ai/x', automation: 'failed' } }]);
  await expect(page.getByTestId('claude.automation-failed')).toContainText('Automatic login failed');
  await page.getByTestId('claude.code').fill('wrong-code');
  await page.getByTestId('claude.code.send').click();
  await expect(page.getByTestId('claude.code.send')).toHaveText('Code sent');
  await answer(user.device, [{ body: { step: 'failed', code: 'code_invalid' }, final: true }]);
  await expect(page.getByTestId('claude.failed')).toHaveText('The login failed: The code was not accepted.');
  // try again, then cancel while waiting: nothing is stored
  await page.getByTestId('claude.retry').click();
  await page.getByTestId('claude.mode.manual').check();
  await page.getByTestId('claude.start').click();
  await expect(page.getByTestId('claude.waiting')).toBeVisible();
  await page.getByTestId('claude.close').click();
  await expect(page.getByTestId('claude.flow')).toHaveCount(0);
  await expect(page.getByTestId('credentials.item.claude_subscription')).toHaveCount(0);
});

// the phone profile without its browser type (a describe group cannot switch the worker's browser)
const { defaultBrowserType: _browser, ...PHONE } = devices['Pixel 7'];

test.describe('on a phone', () => {
  test.use(PHONE);

  test('the settings sections fit the screen and keep touch targets', async ({ page, baseURL }) => {
    await freshUser(page, baseURL!);
    await settings(page);
    await expect(page.getByTestId('settings.model')).toBeVisible();
    await expect(page.getByTestId('settings.quotas')).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    for (const id of ['model.save', 'api-key.save', 'claude.connect', 'mailbox.save', 'quotas.reload']) {
      const box = (await page.getByTestId(id).boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
  });
});
