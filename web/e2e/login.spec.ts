// Sign-in and unlock (06-web.md section 3.4, 04-crypto.md section 5.4): one gesture certifies a new session key and
// unlocks the identity with PRF; a reload needs the passkey again; without PRF the recovery code unlocks once.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator, withoutPrf } from './helpers/authenticator.ts';
import { cancelNext } from './helpers/cancel.ts';
import { onboard } from './helpers/onboard.ts';

test('signing in again after sign-out unlocks with the passkey alone', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  await expect(page.getByTestId('prf-missing.page')).toHaveCount(0);
});

test('a reload keeps the session but needs the passkey to unlock, then returns to the page', async ({
  page,
  baseURL,
}) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.goto('/settings');
  await expect(page).toHaveURL(/\/login\?next=%2Fsettings$/);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByTestId('settings.page')).toBeVisible();
  await page.goto(`/o/${done.org}`);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
});

test('a cancelled sign-in can be tried again', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await cancelNext(page, 'get');
  await page.goto('/login');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('login.error')).toHaveText('Sign-in was cancelled – try again.');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('login.page')).toHaveCount(0);
});

test('without PRF the recovery code unlocks, a wrong code is refused', async ({ page, baseURL }) => {
  const first = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await withoutPrf(page, first);
  await page.goto('/login');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('prf-missing.explain')).toBeVisible();
  await page.getByTestId('prf-missing.recovery.input').fill('AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA');
  await page.getByTestId('prf-missing.recovery.submit').click();
  await expect(page.getByTestId('prf-missing.error')).toHaveText('Cannot decrypt');
  await page.getByTestId('prf-missing.recovery.input').fill(done.recoveryCode.toLowerCase());
  await page.getByTestId('prf-missing.recovery.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
});

test('the recovery prompt can be left by signing out', async ({ page, baseURL }) => {
  const first = await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, code: false });
  await withoutPrf(page, first);
  await page.goto('/login');
  await page.getByTestId('login.submit').click();
  await page.getByTestId('prf-missing.cancel').click();
  await expect(page.getByTestId('login.submit')).toBeVisible();
  await page.goto('/settings');
  await expect(page).toHaveURL(/\/login\?next=/);
});
