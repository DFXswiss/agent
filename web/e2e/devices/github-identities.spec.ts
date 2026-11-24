// GitHub identities (ADR-078, KM-35) against the real hub with the device double: the user's primary account through
// the device flow, a further own account stored by the user's browser under the user's CK, the list in the settings
// with the primary badge, "make primary" and the disconnect of a further account; a company account connected by the
// admin – the device answers the credential body, the browser seals it under a fresh epoch of the organization's
// credential key for its devices only and the hub holds nothing readable – listed by metadata on the organization's
// GitHub screen and removed again.
import { uuid7 } from '../../src/core/encoding/ids';
import { formatTime } from '../../src/core/encoding/time';
import { bodyJson } from '../../src/core/encoding/json';
import { newHeader, sealCredential } from '../../src/core/crypto/credential';
import { unwrapKey } from '../../src/core/crypto/wrap';
import { expect, test } from '../fixtures/test.ts';
import { answerRequest } from '../helpers/operations.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';
import { navigateInApp } from '../helpers/whiteboard.ts';

const BODY = { token: 'gho_not_a_real_token', login: 'octo-two', user_id: 4242, scopes: ['repo'] };

/** A further own account sealed under the user's current CK and stored by the user (`PUT /v1/github-identities`). */
async function storeOwn(api: PageUser): Promise<string> {
  const listed = (await api.request('GET', `/v1/wraps?scope=cred&scope_id=${api.userId}`)) as {
    wraps: { epoch: number; recipient_id: string; wrapped_by: string; wrap: string }[];
  };
  const wrap = listed.wraps
    .filter((item) => item.recipient_id === api.userId && item.wrapped_by === api.userId)
    .sort((a, b) => b.epoch - a.epoch)[0]!;
  const slot = { scope: 'cred', scope_id: api.userId, epoch: wrap.epoch, recipient: api.userId };
  const key = await unwrapKey(api.identity.kem, slot, wrap.wrap);
  const id = uuid7();
  const header = newHeader({
    id,
    owner: api.userId,
    kind: 'github',
    version: 1,
    epoch: wrap.epoch,
    author: api.userId,
    ts: formatTime(Date.now()),
  });
  const body = bodyJson({ ...BODY, created_at: formatTime(Date.now()) });
  const record = await sealCredential(key, header, body, api.identity.sig.privateKey);
  await api.request('PUT', `/v1/github-identities/${id}`, { owner_id: api.userId, record, expected_version: 0 });
  return id;
}

test('lists own accounts, makes a further one primary and disconnects it', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await navigateInApp(page, '/settings');
  await page.getByTestId('github.connect').click();
  await expect(page.getByTestId('github.devices.select')).toHaveValue(user.device);
  await page.getByTestId('github.start').click();
  await expect(page.getByTestId('github.waiting')).toBeVisible();
  await answerRequest(user.device, [
    { body: { step: 'done', login: 'octo-cat' }, final: true, store: { login: 'octo-cat' } },
  ]);
  await expect(page.getByTestId('github.connected')).toHaveText('Connected as octo-cat');
  await expect(page.getByTestId('github.accounts.item')).toHaveCount(1);
  await expect(page.getByTestId('github.accounts.primary')).toBeVisible();
  const second = await storeOwn(api);
  await page.reload();
  await page.getByTestId('login.submit').click();
  const item = page.locator(`[data-testid="github.accounts.item"][data-identity="${second}"]`);
  await expect(item.getByTestId('github.accounts.login')).toHaveText('octo-two');
  await expect(item).toHaveAttribute('data-status', 'verified');
  // the hub holds the further account only encrypted
  const stored = await api.request('GET', `/v1/github-identities/${second}`);
  expect(JSON.stringify(stored)).not.toContain('octo-two');
  await item.getByTestId('github.accounts.make-primary').click();
  await expect(item.getByTestId('github.accounts.primary')).toBeVisible();
  await expect(page.getByTestId('github.connected')).toHaveText('Connected as octo-two');
  const first = page.locator('[data-testid="github.accounts.item"]').filter({ hasText: 'octo-cat' });
  await first.getByTestId('github.accounts.remove').click();
  await page.getByTestId('github.accounts.remove.confirm').click();
  await expect(page.getByTestId('github.accounts.item')).toHaveCount(1);
});

test('connects a company account for the devices of the organization only', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  // the first task assigns the platform device to the default workgroup
  await navigateInApp(page, '/settings');
  await page.getByTestId('github.connect').click();
  await expect(page.getByTestId('github.devices.select')).toHaveValue(user.device);
  await page.getByTestId('github.flow.close').click();
  await navigateInApp(page, `/o/${user.org}/github`);
  await expect(page.getByTestId('company-github.empty')).toBeVisible();
  await page.getByTestId('company-github.add').click();
  await page.getByTestId('company-github.add-panel.device-flow').click();
  await expect(page.getByTestId('github.devices.select')).toHaveValue(user.device);
  await page.getByTestId('github.start').click();
  await expect(page.getByTestId('github.waiting')).toBeVisible();
  const opened = await answerRequest(user.device, [
    {
      body: {
        step: 'done',
        login: 'acme-bot',
        scopes: ['repo'],
        source: 'device_flow',
        body: { ...BODY, login: 'acme-bot', created_at: formatTime(Date.now()) },
      },
      final: true,
    },
  ]);
  expect(opened).toMatchObject({ kind: 'github.connect', body: { org_id: user.org } });
  const item = page.getByTestId('company-github.item');
  await expect(item.getByTestId('company-github.label')).toContainText('acme-bot');
  const listed = (await api.request('GET', '/v1/me/github-identities')) as {
    identities: { id: string; owner_kind: string; epoch: number }[];
  };
  const company = listed.identities.find((identity) => identity.owner_kind === 'org')!;
  expect(company.epoch).toBe(1);
  expect(JSON.stringify(listed)).not.toContain('acme-bot');
  // the workgroup's default: a company account of the organization (or the devices to update first, ADR-078 section 3)
  const me = (await api.request('GET', '/v1/me')) as { orgs: { org_id: string; default_wg: string }[] };
  const wg = me.orgs.find((org) => org.org_id === user.org)!.default_wg;
  await navigateInApp(page, `/o/${user.org}/w/${wg}/settings`);
  const select = page.getByTestId('wg-settings.github.select');
  await select.selectOption(company.id);
  await expect
    .poll(
      async () =>
        (await page.getByTestId('wg-settings.github.failed').count()) > 0 || (await select.inputValue()) === company.id,
    )
    .toBe(true);
  await select.selectOption('');
  await navigateInApp(page, `/o/${user.org}/github`);
  await item.getByTestId('company-github.remove').click();
  await page.getByTestId('company-github.remove.confirm').click();
  await expect(page.getByTestId('company-github.empty')).toBeVisible();
});
