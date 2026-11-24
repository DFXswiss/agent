// GitHub connect and disconnect (06-web.md section 8.1, ADR-015) against the real hub with the device double: a fresh
// private org has no device, so "Connect GitHub" first assigns the platform device; the `github.connect` request is
// sealed to the device (the double opens it), the device code appears with its link and countdown, the device
// stores the credential CK-encrypted and answers `done`; the browser shows the login decrypted from the record.
// Disconnect deletes the record at the hub; the device revokes the token it minted (KOR-40, KOR-65).
import { expect, test } from '../fixtures/test.ts';
import { answerRequest } from '../helpers/operations.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';
import { navigateInApp } from '../helpers/whiteboard.ts';

test('connects GitHub through the device flow and disconnects again', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await page.getByTestId('composer.chip.github').click();
  await expect(page.getByTestId('composer.github-hint')).toBeVisible();
  await navigateInApp(page, '/settings');
  await expect(page.getByTestId('github.disconnected')).toHaveText('No GitHub account is connected.');
  expect(((await api.request('GET', '/v1/me/devices')) as { devices: unknown[] }).devices).toHaveLength(0);
  await page.getByTestId('github.connect').click();
  // the automatic assignment of the first task (no device yet), then the device is preselected
  await expect(page.getByTestId('github.devices.select')).toHaveValue(user.device);
  await page.getByTestId('github.start').click();
  await expect(page.getByTestId('github.waiting')).toBeVisible();
  const opened = await answerRequest(user.device, [
    {
      body: {
        step: 'device_code',
        user_code: 'WDJB-MJHT',
        verification_uri: 'https://github.com/login/device',
        expires_in: 900,
      },
    },
  ]);
  expect(opened).toMatchObject({ kind: 'github.connect', body: {} });
  await expect(page.getByTestId('github.code.value')).toHaveText('WDJB-MJHT');
  await expect(page.getByTestId('github.code.open')).toHaveAttribute('href', 'https://github.com/login/device');
  await expect(page.getByTestId('github.code.expires')).toContainText(/The code expires in 1[45]:\d\d\./);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL! });
  await page.getByTestId('github.code.copy').click();
  await expect(page.getByTestId('github.code.copy')).toHaveText('Copied');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('WDJB-MJHT');
  await answerRequest(user.device, [
    { body: { step: 'done', login: 'octo-cat' }, final: true, store: { login: 'octo-cat' } },
  ]);
  await expect(page.getByTestId('github.connected')).toHaveText('Connected as octo-cat');
  // the hub holds the record only encrypted
  const listed = (await api.request('GET', '/v1/me/credentials')) as { credentials: { kind: string }[] };
  expect(listed.credentials.map((item) => item.kind)).toEqual(['github']);
  const record = await api.request('GET', '/v1/me/credentials/github');
  expect(JSON.stringify(record)).not.toContain('gho_not_a_real_token');
  expect(JSON.stringify(record)).not.toContain('octo-cat');
  // the home composer no longer offers "Connect GitHub"
  await page.getByTestId('shell.nav.home').click();
  await page.getByTestId('composer.chip.github').click();
  await expect(page.getByTestId('composer.github-hint')).toHaveCount(0);
  await page.goto('/settings');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('github.connected')).toHaveText('Connected as octo-cat');
  await page.getByTestId('github.disconnect').click();
  await expect(page.getByTestId('github.disconnect.dialog')).toBeVisible();
  await page.getByTestId('github.disconnect.confirm').click();
  await expect(page.getByTestId('github.disconnected')).toBeVisible();
  // a token of the device flow (here the OAuth App's, KOR-65) is revoked at GitHub by the device: no settings link
  await expect(page.getByTestId('github.disconnected-note')).toHaveAttribute('data-revoked', 'true');
  await expect(page.getByTestId('github.settings-link')).toHaveCount(0);
  expect(((await api.request('GET', '/v1/me/credentials')) as { credentials: unknown[] }).credentials).toHaveLength(0);
});

test('shows the failure the device reports and connects on a retry', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  await page.goto('/settings');
  await page.getByTestId('login.submit').click();
  await page.getByTestId('github.connect').click();
  await page.getByTestId('github.start').click();
  await expect(page.getByTestId('github.waiting')).toBeVisible();
  await answerRequest(user.device, [{ body: { step: 'failed', code: 'upstream_error' }, final: true }]);
  await expect(page.getByTestId('github.failed')).toHaveText('The connection failed: The service refused the request.');
  await page.getByTestId('github.retry').click();
  await page.getByTestId('github.start').click();
  await expect(page.getByTestId('github.waiting')).toBeVisible();
  await answerRequest(user.device, [
    { body: { step: 'done', login: 'octo-cat' }, final: true, store: { login: 'octo-cat' } },
  ]);
  await expect(page.getByTestId('github.connected')).toHaveText('Connected as octo-cat');
});
