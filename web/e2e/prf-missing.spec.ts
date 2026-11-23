// `prf_missing` (06-web.md section 3.3, 04-crypto.md section 5.4) against the real hub for accounts that exist already:
// their passkey signs in from an authenticator that gives no PRF output (`withoutPrf`; a new account needs PRF,
// KOR-22). The guidance unlocks with the recovery code and can remember the browser; a remembered browser unlocks the
// next sign-in without the code; "Forget" in /settings brings the guidance back; the hint to add a PRF passkey from
// another device is shown.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator, withoutPrf } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';

test('without PRF the code unlocks and remembers the browser; forgetting it brings the guidance back', async ({
  page,
  baseURL,
}) => {
  const first = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await withoutPrf(page, first);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('prf-missing.explain')).toHaveText(
    'This passkey cannot open your data on this device.',
  );
  await expect(page.getByTestId('prf-missing.other-device')).toContainText('Add passkey');
  await page.getByTestId('prf-missing.recovery.input').fill(done.recoveryCode);
  await page.getByTestId('prf-missing.remember').check();
  await page.getByTestId('prf-missing.recovery.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
  // the remembered browser unlocks the next sign-in without the code
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  // forget: the next sign-in needs the recovery code again
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('passkeys.browser.forget').click();
  await expect(page.getByTestId('passkeys.browser.none')).toBeVisible();
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('prf-missing.explain')).toBeVisible();
});

test('remembering the browser from the settings', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, code: false });
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.browser.none')).toBeVisible();
  await page.getByTestId('passkeys.browser.remember').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
});
