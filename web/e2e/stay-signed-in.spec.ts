// "Stay signed in" (rule E-37, ADR-099; 06-web.md sections 2.3, 3.3, 3.4) against the real hub: after the sign-in the
// browser stays signed in and unlocked – a reload, a new tab, a closed and reopened tab and a restarted browser open
// without the passkey; a session about to end renews itself without a gesture; with "Stay signed in" off a reload is
// locked as before; signing out removes the session and this browser's device-key entry.
import { chromium, type Page } from '@playwright/test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator, type VirtualAuthenticator } from './helpers/authenticator.ts';
import { directorOrigin } from './fixtures/env.ts';
import { onboard } from './helpers/onboard.ts';

test.use({ staySignedIn: true });

const DAY_MS = 24 * 3_600_000;

interface StoredSession {
  readonly userId: string;
  readonly expiresAt: number;
  readonly renewedAt?: number;
}

/** The session entry of the page's IndexedDB (`core/persist`), null without one. */
function storedSession(page: Page): Promise<StoredSession | null> {
  return page.evaluate(
    () =>
      new Promise<StoredSession | null>((resolve, reject) => {
        const open = indexedDB.open('agent-platform');
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const request = open.result.transaction('session').objectStore('session').get('current');
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const value = request.result as StoredSession | undefined;
            open.result.close();
            resolve(value === undefined ? null : value);
          };
        };
      }),
  );
}

/** The users with a device-key entry in the page's IndexedDB (`core/persist/device-key.ts`). */
function deviceEntries(page: Page): Promise<string[]> {
  return page.evaluate(
    () =>
      new Promise<string[]>((resolve, reject) => {
        const open = indexedDB.open('agent-platform-device');
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const request = open.result.transaction('keys').objectStore('keys').getAllKeys();
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            open.result.close();
            resolve(request.result.map(String));
          };
        };
      }),
  );
}

/** The stored session ends `ms` from now and was last renewed long ago (what an old browser state looks like). */
function endStoredSessionIn(page: Page, ms: number): Promise<void> {
  return page.evaluate(
    (left) =>
      new Promise<void>((resolve, reject) => {
        const open = indexedDB.open('agent-platform');
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const store = open.result.transaction('session', 'readwrite').objectStore('session');
          const request = store.get('current');
          request.onsuccess = () => {
            const put = store.put({ ...request.result, expiresAt: Date.now() + left, renewedAt: 0 }, 'current');
            put.onerror = () => reject(put.error);
            put.onsuccess = () => {
              open.result.close();
              resolve();
            };
          };
        };
      }),
    ms,
  );
}

/** The signature counter of the passkey: it grows with every passkey gesture. */
async function gestures(authenticator: VirtualAuthenticator): Promise<number> {
  const [credential] = (await authenticator.credentials()) as { signCount: number }[];
  return credential!.signCount;
}

async function expectUnlocked(page: Page, org: string): Promise<void> {
  await expect(page).toHaveURL(new RegExp(`/o/${org}$`));
  await expect(page.getByTestId('login.page')).toHaveCount(0);
  await expect(page.getByTestId('shell.settings')).toBeVisible();
}

test('signed in, a reload, a new tab and a closed and reopened tab open unlocked without the passkey', async ({
  page,
  context,
  baseURL,
}) => {
  const authenticator = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL!, code: false });
  // the sign-in renewed the session at once: it now lasts about 30 days
  await expect.poll(async () => (await storedSession(page))?.renewedAt ?? 0).toBeGreaterThan(0);
  expect((await storedSession(page))!.expiresAt).toBeGreaterThan(Date.now() + 29 * DAY_MS);
  const before = await gestures(authenticator);
  await page.reload();
  await expectUnlocked(page, done.org);
  expect(await gestures(authenticator)).toBe(before);
  // a second tab of the same browser (it has no authenticator: a passkey request would fail)
  const second = await context.newPage();
  await second.goto('/');
  await expectUnlocked(second, done.org);
  // every tab closed, then the site opened again
  await page.close();
  await second.close();
  const reopened = await context.newPage();
  await reopened.goto(`/o/${done.org}`);
  await expectUnlocked(reopened, done.org);
  await reopened.getByTestId('shell.settings').click();
  await expect(reopened.getByTestId('passkeys.browser.remembered')).toBeVisible();
  await expect(reopened.getByTestId('passkeys.browser.forget')).toHaveText('Forget on this browser');
});

test('a restarted browser with its stored state opens unlocked without the passkey', async ({ baseURL }) => {
  const profile = mkdtempSync(join(tmpdir(), 'agent-stay-'));
  try {
    const first = await chromium.launchPersistentContext(profile, { baseURL: baseURL! });
    const page = first.pages()[0] ?? (await first.newPage());
    await addAuthenticator(page);
    const done = await onboard(page, { origin: baseURL!, code: false });
    await first.close();
    // the same profile again: a new browser process with the stored IndexedDB, no authenticator
    const second = await chromium.launchPersistentContext(profile, { baseURL: baseURL! });
    const again = second.pages()[0] ?? (await second.newPage());
    await again.goto('/');
    await expectUnlocked(again, done.org);
    await second.close();
  } finally {
    rmSync(profile, { recursive: true, force: true });
  }
});

test('a session about to end renews itself without a gesture', async ({ page, baseURL }) => {
  const authenticator = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL!, code: false });
  const { userId } = (await storedSession(page))!;
  // the hub's session and the browser's copy end in two minutes, the last renewal is long ago
  const response = await fetch(`${directorOrigin()}/users/${userId}/sessions/expiring`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ seconds: 120 }),
  });
  expect(await response.json()).toEqual({ sessions: 1 });
  await endStoredSessionIn(page, 120_000);
  const before = await gestures(authenticator);
  const renewed = page.waitForResponse(
    (answer) => answer.url().endsWith('/v1/session/renew') && answer.status() === 200,
  );
  await page.reload();
  await renewed;
  await expectUnlocked(page, done.org);
  await expect.poll(async () => (await storedSession(page))!.expiresAt).toBeGreaterThan(Date.now() + 29 * DAY_MS);
  await expect(page.getByTestId('shell.renew')).toHaveCount(0);
  expect(await gestures(authenticator)).toBe(before);
});

test('with "Stay signed in" off a reload is locked as before', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL!, code: false });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByTestId('login.stay')).toBeChecked();
  await page.getByTestId('login.stay').uncheck();
  await page.getByTestId('login.submit').click();
  await expectUnlocked(page, done.org);
  expect(await deviceEntries(page)).toEqual([]);
  await page.reload();
  await expect(page).toHaveURL(/\/login\?next=/);
  // the choice is kept for this browser
  await expect(page.getByTestId('login.stay')).not.toBeChecked();
  await page.getByTestId('login.submit').click();
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.browser.none')).toBeVisible();
  // "Stay signed in on this browser" in the settings turns it on again
  await page.getByTestId('passkeys.browser.remember').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('settings.page')).toBeVisible();
  // "Forget on this browser": the next reload asks for the passkey again
  await page.getByTestId('passkeys.browser.forget').click();
  await expect(page.getByTestId('passkeys.browser.none')).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/\/login\?next=%2Fsettings$/);
});

test('signing out removes the session and this browser device-key entry', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, code: false });
  const { userId } = (await storedSession(page))!;
  expect(await deviceEntries(page)).toEqual([userId]);
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await expect(page).toHaveURL(/\/login$/);
  expect(await storedSession(page)).toBeNull();
  expect(await deviceEntries(page)).toEqual([]);
  await page.goto('/settings');
  await expect(page).toHaveURL(/\/login\?next=%2Fsettings$/);
});
