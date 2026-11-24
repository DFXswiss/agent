// /settings (06-web.md sections 2.5, 2.6): the language is saved in the user's settings and follows the account;
// sign-out ends the session at the hub and deletes the session entries of IndexedDB.
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';

test('the language is saved for the account', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('settings.page')).toContainText('Settings');
  await page.getByTestId('settings.language.select').selectOption('de');
  await expect(page.getByTestId('settings.language.saved')).toHaveText('Gespeichert');
  await expect(page.getByTestId('settings.page')).toContainText('Einstellungen');
  expect(await page.evaluate(() => document.documentElement.lang)).toBe('de');
  await page.reload();
  await expect(page.getByTestId('login.page')).toContainText('Sign in');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('settings.page')).toContainText('Einstellungen');
});

test('a refused save is reported', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.route('**/v1/me/settings', (route) => route.fulfill({ status: 503, json: { error: 'unavailable' } }));
  await page.getByTestId('settings.language.select').selectOption('de');
  await expect(page.getByTestId('settings.language.error')).toBeVisible();
});

test('sign-out ends the session at the hub and clears the browser', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  const sessions = () =>
    page.evaluate(
      () =>
        new Promise<number>((resolve, reject) => {
          const open = indexedDB.open('agent-platform');
          open.onerror = () => reject(open.error);
          open.onsuccess = () => {
            const request = open.result.transaction('session').objectStore('session').count();
            request.onsuccess = () => {
              open.result.close();
              resolve(request.result);
            };
          };
        }),
    );
  expect(await sessions()).toBe(1);
  await page.getByTestId('shell.settings').click();
  const ended = page.waitForResponse((response) => response.url().endsWith('/v1/session/end'));
  await page.getByTestId('settings.sign-out').click();
  expect((await ended).status()).toBe(200);
  await expect(page).toHaveURL(/\/login$/);
  expect(await sessions()).toBe(0);
  await page.goto('/settings');
  await expect(page).toHaveURL(/\/login\?next=%2Fsettings$/);
});
