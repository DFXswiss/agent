// The app shell (06-web.md sections 2.4, 2.6, 12.1, 12.5, 12.6): header and sign-in link, guarded routes, the
// connection banner (offline, reconnecting), the "Sign in again" prompt after `session_expired`, mobile layout.
import { devices } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';

test('anonymous visitors see the sign-in link and are sent to the sign-in page', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByTestId('shell.signin')).toBeVisible();
  await expect(page.getByTestId('shell.settings')).toHaveCount(0);
  await page.goto('/o/0190f000-0000-7000-8000-000000000000/w/x');
  await expect(page).toHaveURL(/\/login\?next=%2Fo%2F0190f000-0000-7000-8000-000000000000%2Fw%2Fx$/);
  await page.goto('/nowhere');
  await expect(page.getByTestId('notfound.title')).toBeVisible();
  await expect(page.getByTestId('shell.header')).toBeVisible();
});

test('the connection banner shows offline and reconnecting', async ({ page, context, baseURL }) => {
  await addAuthenticator(page);
  // a stream that ends at once: the channel reconnects with backoff
  await page.route('**/v1/stream**', (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: ': hi\n\n' }),
  );
  await onboard(page, { origin: baseURL! });
  await expect(page.getByTestId('shell.connection.reconnecting')).toBeVisible();
  await context.setOffline(true);
  await expect(page.getByTestId('shell.connection.offline')).toHaveText('Offline');
  await context.setOffline(false);
  await expect(page.getByTestId('shell.connection.offline')).toHaveCount(0);
});

test('an expired session offers to sign in again without leaving the page', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.route('**/v1/me/settings', (route) => route.fulfill({ status: 401, json: { error: 'session_expired' } }), {
    times: 1,
  });
  await page.getByTestId('settings.language.select').selectOption('de');
  await expect(page.getByTestId('shell.renew.text')).toHaveText('Your session has ended.');
  // the first attempt is cancelled in the passkey dialog
  await page.evaluate(() => {
    const credentials = navigator.credentials;
    const original = credentials.get.bind(credentials);
    credentials.get = () => {
      credentials.get = original;
      return Promise.reject(new DOMException('cancelled', 'NotAllowedError'));
    };
  });
  await page.getByTestId('shell.renew.submit').click();
  await expect(page.getByTestId('shell.renew.message')).toHaveText('Sign-in was cancelled – try again.');
  await page.getByTestId('shell.renew.submit').click();
  await expect(page.getByTestId('shell.renew')).toHaveCount(0);
  await expect(page.getByTestId('settings.page')).toBeVisible();
  await page.getByTestId('settings.language.select').selectOption('de');
  await expect(page.getByTestId('settings.language.saved')).toBeVisible();
});

// Pixel 7 without `defaultBrowserType` (a describe group cannot switch the browser; it is Chromium anyway)
const { defaultBrowserType: _browser, ...pixel7 } = devices['Pixel 7'];

test('an org whose log does not extend the pinned head is blocked with its evidence', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.reload();
  await expect(page.getByTestId('login.page')).toBeVisible();
  // another head pinned at seq 0: the served log no longer extends it (L9, log_rollback)
  await page.evaluate(
    (org) =>
      new Promise<void>((resolve, reject) => {
        const open = indexedDB.open('agent-platform');
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const store = open.result.transaction('heads', 'readwrite').objectStore('heads');
          const read = store.get(org);
          read.onsuccess = () => {
            const write = store.put({ ...read.result, head: { seq: 0, hash: 'forged' } }, org);
            write.onsuccess = () => {
              open.result.close();
              resolve();
            };
          };
        };
      }),
    done.org,
  );
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('shell.blocked')).toBeVisible();
  await expect(page.getByTestId('shell.blocked.code')).toHaveText('Error code: log_rollback');
  await expect(page.getByTestId('shell.blocked.evidence')).toContainText('stored head 0:forged');
});

test('a browser that refuses localStorage still works, without the remembered preferences', async ({
  page,
  baseURL,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {
      get() {
        throw new DOMException('denied', 'SecurityError');
      },
    });
  });
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, skip: true });
  await expect(page.getByTestId('shell.recovery-banner')).toBeVisible();
});

test.describe('on a phone', () => {
  test.use(pixel7);

  test('the whole onboarding works on a small screen', async ({ page, baseURL }) => {
    await addAuthenticator(page);
    await onboard(page, { origin: baseURL! });
    await expect(page.getByTestId('shell.header')).toBeVisible();
  });
});
