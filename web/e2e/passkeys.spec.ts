// Passkeys in /settings (06-web.md section 3.6, 04-crypto.md section 5.6) against the real hub with a second virtual
// authenticator (`transport: "usb"`, a roaming authenticator as the platform offers it "on another device"): the new
// passkey unlocks the same identity on its own, the old one is removed, the last one cannot be removed (also not when
// the list is stale: `last_passkey`), removing the passkey of this session signs out. The refused user verification
// runs last (07-testing.md section 1).
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator, type VirtualAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

async function remove(authenticator: VirtualAuthenticator): Promise<void> {
  await authenticator.cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: authenticator.id });
}

test('a second passkey on another device unlocks the same identity, the first is removed', async ({
  page,
  baseURL,
}) => {
  const platform = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  const usb = await addAuthenticator(page, { transport: 'usb' });
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.item')).toHaveCount(1);
  await expect(page.getByTestId('passkeys.last')).toBeVisible();
  await page.getByTestId('passkeys.add.other-device').click();
  await expect(page.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'confirm');
  expect(await usb.credentials()).toHaveLength(1);
  expect(await platform.credentials()).toHaveLength(1);
  await page.getByTestId('passkeys.add.confirm').click();
  await expect(page.getByTestId('passkeys.add.notice')).toHaveText(
    'The passkey was added. It signs in and unlocks your keys.',
  );
  const items = page.getByTestId('passkeys.item');
  await expect(items).toHaveCount(2);
  await expect(items.and(page.locator('[data-prf="true"]'))).toHaveCount(2);
  // this browser continues with the session of the new passkey
  const current = page.getByTestId('passkeys.item').filter({ has: page.getByTestId('passkeys.item.current') });
  await expect(current).toHaveCount(1);
  const usbId = await current.getAttribute('data-credential');
  // the first passkey is removed (after a confirmation that can be cancelled)
  const first = items.filter({ hasNot: page.getByTestId('passkeys.item.current') });
  await first.getByTestId('passkeys.remove').click();
  await page.getByTestId('passkeys.remove.cancel').click();
  await expect(page.getByTestId('passkeys.remove-dialog')).toHaveCount(0);
  await first.getByTestId('passkeys.remove').click();
  await expect(page.getByTestId('passkeys.remove-dialog')).toContainText('Your other passkeys still sign you in');
  await page.getByTestId('passkeys.remove.confirm').click();
  await expect(page.getByTestId('passkeys.notice')).toHaveText('The passkey was removed.');
  await expect(items).toHaveCount(1);
  await expect(items).toHaveAttribute('data-credential', usbId!);
  await expect(page.getByTestId('passkeys.last')).toBeVisible();
  // without the platform authenticator, the usb passkey alone signs in and unlocks
  await remove(platform);
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  await expect(page.getByTestId('prf-missing.page')).toHaveCount(0);
});

test('the hub refuses the last passkey when the list is stale, and removing this session signs out', async ({
  page,
  baseURL,
}) => {
  const platform = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  const usb = await addAuthenticator(page, { transport: 'usb' });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('passkeys.add.other-device').click();
  await page.getByTestId('passkeys.add.confirm').click();
  await expect(page.getByTestId('passkeys.item')).toHaveCount(2);
  // the roaming authenticator already holds a passkey of the account
  await page.getByTestId('passkeys.add.other-device').click();
  await expect(page.getByTestId('passkeys.add.notice')).toHaveText(
    'This authenticator already holds a passkey of your account. Use another one.',
  );
  const stale = page.getByTestId('passkeys.item').filter({ hasNot: page.getByTestId('passkeys.item.current') });
  const staleId = (await stale.getAttribute('data-credential'))!;
  // another device removes the first passkey; this page still lists it
  const user = await PageUser.open(page, done.recoveryCode);
  await user.request('DELETE', `/v1/passkeys/${staleId}`);
  const current = page.getByTestId('passkeys.item').filter({ has: page.getByTestId('passkeys.item.current') });
  await current.getByTestId('passkeys.remove').click();
  await expect(page.getByTestId('passkeys.remove-dialog')).toContainText('This passkey signed in this browser.');
  await page.getByTestId('passkeys.remove.confirm').click();
  await expect(page.getByTestId('passkeys.notice')).toHaveText('Last passkey');
  await expect(page.getByTestId('passkeys.item')).toHaveCount(1);
  // a further passkey on a second roaming authenticator; then the passkey of this session goes: the browser signs out
  await remove(usb);
  const spare = await addAuthenticator(page, { transport: 'usb' });
  await page.getByTestId('passkeys.add.other-device').click();
  await page.getByTestId('passkeys.add.confirm').click();
  await expect(page.getByTestId('passkeys.item')).toHaveCount(2);
  expect(await spare.credentials()).toHaveLength(1);
  const now = page.getByTestId('passkeys.item').filter({ has: page.getByTestId('passkeys.item.current') });
  await now.getByTestId('passkeys.remove').click();
  await page.getByTestId('passkeys.remove.confirm').click();
  await expect(page).toHaveURL(/\/login$/);
  expect(await platform.credentials()).toHaveLength(1);
});

test('adding a passkey reports a refused user verification', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  // last: Chromium keeps a refused user verification for later ceremonies of the page (Spike D)
  const refusing = await addAuthenticator(page, { transport: 'usb', isUserVerified: false });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('passkeys.add.other-device').click();
  await expect(page.getByTestId('passkeys.add.notice')).toHaveText('The passkey ceremony was cancelled – try again.');
  await expect(page.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'idle');
  expect(await refusing.credentials()).toHaveLength(0);
  await expect(page.getByTestId('passkeys.item')).toHaveCount(1);
});
