// `prf_missing` (06-web.md section 3.3, 04-crypto.md section 5.4) with the `hasPrf: false` authenticator against the real
// hub: the registration offers "Remember this browser" right after the recovery code; a remembered browser unlocks the
// sign-in without the recovery code; "Forget" in /settings brings the guidance back, whose recovery-code unlock can
// remember the browser again; the hint to add a PRF passkey from another device is shown.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';

test('a remembered browser unlocks without PRF; forgetting it brings the guidance back', async ({ page, baseURL }) => {
  await addAuthenticator(page, { hasPrf: false });
  const done = await onboard(page, { origin: baseURL!, remember: true });
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
  await expect(page.getByTestId('passkeys.item.prf')).toHaveText('Cannot unlock your encryption keys (no PRF)');
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  // forget: the next sign-in needs the recovery code again
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('passkeys.browser.forget').click();
  await expect(page.getByTestId('passkeys.browser.none')).toBeVisible();
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('prf-missing.explain')).toHaveText(
    'This passkey cannot unlock your encryption keys on this device.',
  );
  await expect(page.getByTestId('prf-missing.other-device')).toContainText('Add passkey');
  await page.getByTestId('prf-missing.recovery.input').fill(done.recoveryCode);
  await page.getByTestId('prf-missing.remember').check();
  await page.getByTestId('prf-missing.recovery.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
});

test('remembering the browser from the settings and confirming a skipped recovery code', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL!, skip: true });
  await expect(page.getByTestId('shell.recovery-banner')).toBeVisible();
  await page.getByTestId('shell.recovery-banner.settings').click();
  await expect(page.getByTestId('passkeys.recovery.unconfirmed')).toBeVisible();
  await page.getByTestId('passkeys.recovery.input').fill('AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA');
  await page.getByTestId('passkeys.recovery.submit').click();
  await expect(page.getByTestId('passkeys.recovery.error')).toHaveText('This is not your recovery code.');
  await page.getByTestId('passkeys.recovery.input').fill('AAAAA');
  await page.getByTestId('passkeys.recovery.submit').click();
  await expect(page.getByTestId('passkeys.recovery.error')).not.toHaveText('This is not your recovery code.');
  await page.getByTestId('passkeys.recovery.input').fill(done.recoveryCode.toLowerCase());
  await page.getByTestId('passkeys.recovery.submit').click();
  await expect(page.getByTestId('passkeys.recovery.ok')).toHaveText('Your recovery code is correct.');
  await expect(page.getByTestId('passkeys.recovery.unconfirmed')).toHaveCount(0);
  await expect(page.getByTestId('shell.recovery-banner')).toHaveCount(0);
  // this browser can also be remembered from the settings
  await expect(page.getByTestId('passkeys.browser.none')).toBeVisible();
  await page.getByTestId('passkeys.browser.remember').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
});
