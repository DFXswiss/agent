// The error pages of 06-web.md section 12.6 against the real hub: not found and forbidden look the same ("not
// available", nothing revealed), the hub unavailable at start (503) with "Reload", the unsupported browser with what
// is missing and the supported browsers; the blocking org state is covered by `shell.spec.ts`, session expiry by the
// Vitest suite (the hub's session lifetime is 12 h).
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';
import { navigateInApp } from './helpers/whiteboard.ts';

test('an unknown task and a foreign page show the same neutral page', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const user = await onboard(page, { origin: baseURL! });
  await navigateInApp(page, `/o/${user.org}/t/0190f000-0000-7000-8000-000000000000`);
  const task = page.getByTestId('task.error');
  await expect(task.getByTestId('not-available.title')).toHaveText('Not available');
  const unknown = await task.innerText();
  // the platform invitations of someone who is no inviter: the same page, no hint that the page exists
  await navigateInApp(page, '/platform/invites');
  const invites = page.getByTestId('platform-invites.forbidden');
  await expect(invites.getByTestId('not-available.title')).toHaveText('Not available');
  expect(await invites.innerText()).toBe(unknown);
  await invites.getByTestId('not-available.home').click();
  await expect(page).toHaveURL(new RegExp(`/o/${user.org}$`));
});

test('a screen that cannot load shows the code and loads again on "Try again"', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const user = await onboard(page, { origin: baseURL! });
  const unknown = '0190f000-0000-7000-8000-000000000001';
  for (const [kind, path, testId] of [
    ['tasks', `/o/${user.org}/t/${unknown}`, 'task.error'],
    ['sessions', `/o/${user.org}/s/${unknown}`, 'session.error'],
  ] as const) {
    await page.route(`**/v1/${kind}/${unknown}`, (route) =>
      route.fulfill({ status: 503, json: { error: 'unavailable' } }),
    );
    await navigateInApp(page, path);
    const screen = page.getByTestId(testId);
    await expect(screen.getByTestId('load-error.code')).toHaveText('Error code: unavailable');
    await page.unroute(`**/v1/${kind}/${unknown}`);
    await screen.getByTestId('load-error.retry').click();
    // the hub answers now: the item does not exist, which looks like any page one may not see
    await expect(screen.getByTestId('not-available.title')).toHaveText('Not available');
  }
});

test('an unavailable hub at start shows the page with the code and "Reload"', async ({ page }) => {
  await page.route('**/v1/config', (route) => route.fulfill({ status: 503, json: { error: 'unavailable' } }));
  await page.goto('/login');
  await expect(page.getByTestId('error.title')).toHaveText('Service unavailable');
  await expect(page.getByTestId('error.code')).toHaveText('Error code: unavailable');
  await expect(page.getByTestId('error.action')).toHaveText('Try again in a few minutes.');
  await page.unroute('**/v1/config');
  await page.getByTestId('error.reload').click();
  await expect(page.getByTestId('login.page')).toBeVisible();
});

test('a browser without passkeys or Ed25519 gets the unsupported page with what is missing', async ({ page }) => {
  await page.addInitScript({
    content: `delete window.PublicKeyCredential;
      const generate = crypto.subtle.generateKey.bind(crypto.subtle);
      crypto.subtle.generateKey = (algorithm, ...rest) =>
        algorithm.name === 'Ed25519' ? Promise.reject(new DOMException('no', 'NotSupportedError')) : generate(algorithm, ...rest);`,
  });
  await page.goto('/login');
  await expect(page.getByTestId('unsupported.title')).toHaveText('This browser is not supported');
  await expect(page.getByTestId('unsupported.missing')).toHaveText(
    'Missing in this browser: passkeys (WebAuthn), Ed25519 in WebCrypto.',
  );
  await expect(page.getByTestId('unsupported.browsers').getByRole('listitem')).toHaveCount(4);
  await expect(page.getByTestId('login.page')).toHaveCount(0);
});
