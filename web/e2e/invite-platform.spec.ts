// Platform invitation → passkey with PRF → private org, no code screen (06-web.md sections 3.1, 3.1.1, 3.2;
// 04-crypto.md sections 5.1, 5.3; KOR-22; E2E-01 web part) against the real hub with a CDP virtual authenticator.
import { formatCode, generateCode } from '../src/core/crypto/invite';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { cancelNext } from './helpers/cancel.ts';
import { onboard, ORG_PATH } from './helpers/onboard.ts';
import { platformInvite } from './helpers/platform.ts';

test('a new customer registers with a passkey and lands in the private organization, no code screen', async ({
  page,
  baseURL,
}) => {
  const authenticator = await addAuthenticator(page);
  const sent: string[] = [];
  page.on('request', (request) => sent.push(`${request.url()}\n${request.postData() ?? ''}`));
  const invite = await platformInvite(baseURL!);
  await page.goto(`/invite#${invite.code}`);
  // the fragment is read once and removed from the address
  await expect(page.getByTestId('invite.display-name')).toBeVisible();
  expect(new URL(page.url()).hash).toBe('');
  expect(await page.evaluate(() => window.location.href)).not.toContain('#');
  await page.getByTestId('invite.create-passkey').click();
  await expect(page.getByTestId('invite.display-name.invalid')).toBeVisible();
  await page.getByTestId('invite.display-name').fill('Ada Lovelace');
  await page.getByTestId('invite.create-passkey').click();
  await expect(page.getByTestId('invite.unlock')).toBeVisible();
  expect(await authenticator.credentials()).toHaveLength(1);
  await page.getByTestId('invite.unlock').click();
  await expect(page).toHaveURL(ORG_PATH);
  await expect(page.getByTestId('recovery-code.step')).toHaveCount(0);
  await expect(page.getByTestId('shell.blocked')).toHaveCount(0);
  await expect(page.getByTestId('shell.settings')).toBeVisible();
  // `/` brings the user back to the private org
  const org = ORG_PATH.exec(page.url())![1]!;
  await page.getByTestId('shell.brand').click();
  await expect(page).toHaveURL(new RegExp(`/o/${org}$`));
  // the invitation code never left the browser, and no recovery code was set afterwards
  const traffic = sent.join('\n');
  for (const secret of [invite.code, invite.code.replace(/-/g, '')]) expect(traffic).not.toContain(secret);
  expect(traffic).not.toContain('/v1/me/bundles/recovery');
  expect(traffic).not.toContain('/v1/me/recovery/begin');
});

test('the same invitation cannot be used twice', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.goto(`/invite#${done.code}`);
  await expect(page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
});

test('a cancelled passkey creation returns to the intro and the next attempt registers', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await cancelNext(page, 'create');
  const invite = await platformInvite(baseURL!);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await expect(page.getByTestId('invite.notice')).toHaveText('Passkey creation was cancelled – try again.');
  await expect(page.getByTestId('invite.display-name')).toHaveValue('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await expect(page.getByTestId('invite.unlock')).toBeVisible();
});

test('a cancelled second gesture can be repeated without registering again', async ({ page, baseURL }) => {
  const authenticator = await addAuthenticator(page);
  await cancelNext(page, 'get');
  const invite = await platformInvite(baseURL!);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  await expect(page.getByTestId('invite.notice')).toHaveText('Sign-in was cancelled – try again.');
  await page.getByTestId('invite.unlock').click();
  await expect(page).toHaveURL(ORG_PATH);
  expect(await authenticator.credentials()).toHaveLength(1);
});

test('a malformed or unknown code is reported without contacting the registration', async ({ page }) => {
  await page.goto('/invite#ABCDE-ABCDE');
  await expect(page.getByTestId('invite.malformed')).toHaveAttribute('data-code', 'invite_code_length');
  await expect(page.getByTestId('invite.malformed')).toContainText('Check the code');
  // no code in the tab (e.g. reloaded): the screen says so and that nothing was joined (KOR-36)
  await page.goto('/invite');
  await expect(page.getByTestId('invite.missing')).toBeVisible();
  await page.goto('/nowhere');
  await page.goto('/invite#ABCDE-ABCDE-ABCDE-ABCDE-ABCDE1');
  await expect(page.getByTestId('invite.malformed')).toHaveAttribute('data-code', 'invite_code_invalid');
  await page.goto('/nowhere');
  await page.goto(`/invite#${formatCode(generateCode())}`);
  await expect(page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
  await expect(page.getByTestId('invite.invalid')).toContainText('Invitation not valid');
});

test('an unavailable hub can be retried', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const invite = await platformInvite(baseURL!);
  await page.route('**/v1/invites/lookup', (route) => route.fulfill({ status: 503, json: { error: 'unavailable' } }));
  await page.goto(`/invite#${invite.code}`);
  await expect(page.getByTestId('invite.error')).toHaveAttribute('data-code', 'unavailable');
  await page.unroute('**/v1/invites/lookup');
  await page.getByTestId('invite.retry').click();
  await expect(page.getByTestId('invite.display-name')).toBeVisible();
});

test('a registered but unfinished invitation and an org invitation take their own branches', async ({ page }) => {
  const answer = { kind: 'user', expires_at: '2030-01-01T00:00:00.000Z' };
  await page.route('**/v1/invites/lookup', (route) =>
    route.fulfill({ json: { ...answer, scope: 'platform', registered: true } }),
  );
  await page.goto(`/invite#${formatCode(generateCode())}`);
  await expect(page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_registered');
  await page.unroute('**/v1/invites/lookup');
  await page.route('**/v1/invites/lookup', (route) =>
    route.fulfill({
      json: { ...answer, scope: 'org', registered: false, org_id: '0190f000-0000-7000-8000-000000000000' },
    }),
  );
  await page.goto('/nowhere');
  await page.goto(`/invite#${formatCode(generateCode())}`);
  await expect(page.getByTestId('invite.org')).toBeVisible();
});

test('a failed organization creation is retried with the same request', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const bodies: string[] = [];
  let fail = true;
  await page.route('**/v1/orgs', async (route) => {
    bodies.push(route.request().postData()!);
    if (fail) {
      fail = false;
      await route.fulfill({ status: 503, json: { error: 'unavailable' } });
    } else {
      await route.continue();
    }
  });
  const invite = await platformInvite(baseURL!);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  await expect(page.getByTestId('invite.accept-failed')).toHaveAttribute('data-code', 'unavailable');
  await page.getByTestId('invite.accept-retry').click();
  await expect(page).toHaveURL(ORG_PATH);
  expect(bodies).toHaveLength(2);
  expect(bodies[1]).toBe(bodies[0]);
});

test('a passkey without PRF is refused with the hint; nothing remains and the same link works with another', async ({
  page,
  baseURL,
}) => {
  const refused = await addAuthenticator(page, { hasPrf: false });
  const sent: string[] = [];
  page.on('request', (request) => sent.push(`${request.method()} ${request.url()}`));
  const invite = await platformInvite(baseURL!);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  const message = page.getByTestId('prf-required');
  await expect(message).toContainText('You need a passkey that supports PRF.');
  await expect(message).toContainText('Bitwarden currently does not support PRF');
  await expect(message).toContainText('Delete the passkey you just created');
  await expect(page.getByTestId('prf-required.later')).toHaveCount(0);
  // the registration was withdrawn at the hub; no passkey slot, no organization
  expect(sent.some((line) => line.startsWith('DELETE') && line.endsWith('/v1/me/registration'))).toBe(true);
  expect(sent.some((line) => line.includes('/v1/me/bundles/'))).toBe(false);
  expect(sent.some((line) => line.startsWith('POST') && line.endsWith('/v1/orgs'))).toBe(false);
  // another passkey on the same link
  await refused.cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: refused.id });
  await addAuthenticator(page);
  await page.getByTestId('prf-required.retry').click();
  await expect(page.getByTestId('invite.display-name')).toHaveValue('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  await expect(page).toHaveURL(ORG_PATH);
});
