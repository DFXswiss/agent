// The recovery code (06-web.md section 3.2, KOR-22): a registration with PRF shows none and nothing reminds of it but one
// quiet line in /settings; there "Create recovery code" shows the code with copy, download as text file and one
// "Done" (a passkey gesture saves it); a new code replaces the old one.
import { readFileSync } from 'node:fs';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { navigateInApp, onboard } from './helpers/onboard.ts';

test('no code after a PRF registration: one quiet line in the settings, nothing in the shell', async ({
  page,
  baseURL,
}) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, code: false });
  await expect(page.getByTestId('shell.layout')).not.toContainText('recovery code');
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.recovery')).toHaveAttribute('data-status', 'none');
  await expect(page.getByTestId('passkeys.recovery.none')).toHaveText('You have no recovery code yet.');
  await expect(page.getByTestId('passkeys.recovery.create')).toHaveText('Create recovery code');
});

test('the code can be created, copied and downloaded, and finds the account', async ({ page, context, baseURL }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL!, code: false });
  await navigateInApp(page, '/settings');
  await page.getByTestId('passkeys.recovery.create').click();
  const code = (await page.getByTestId('recovery-code.code').textContent())!;
  expect(code).toMatch(/^[A-Z2-7]{5}(-[A-Z2-7]{5}){3}-[A-Z2-7]{6}$/);
  await expect(page.getByTestId('recovery-code.step').getByRole('textbox')).toHaveCount(0);
  await page.getByTestId('recovery-code.copy').click();
  await expect(page.getByTestId('recovery-code.copied')).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(code);
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByTestId('recovery-code.download').click(),
  ]);
  expect(download.suggestedFilename()).toBe('agent-recovery-code.txt');
  expect(readFileSync((await download.path())!, 'utf8')).toContain(code);
  await expect(page.getByTestId('recovery-code.continue')).toHaveText('Done');
  await page.getByTestId('recovery-code.continue').click();
  await expect(page.getByTestId('passkeys.recovery.done')).toHaveText('Your recovery code is saved.');
  await expect(page.getByTestId('passkeys.recovery.none')).toHaveCount(0);
  await page.getByTestId('settings.sign-out').click();
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(code);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.passkey-step')).toBeVisible();
});

test('a refused clipboard asks to copy by hand', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    });
  });
  await onboard(page, { origin: baseURL!, code: false });
  await navigateInApp(page, '/settings');
  await page.getByTestId('passkeys.recovery.create').click();
  await page.getByTestId('recovery-code.copy').click();
  await expect(page.getByTestId('recovery-code.copy-failed')).toBeVisible();
});

test('a new recovery code from the settings replaces the old one (KOR-01)', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.recovery')).toHaveAttribute('data-status', 'set');
  await page.getByTestId('passkeys.recovery.create').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  expect(fresh).not.toBe(done.recoveryCode);
  await expect(page.getByTestId('recovery-code.explain')).toContainText('Until then your current code stays valid.');
  await page.getByTestId('recovery-code.continue').click();
  await expect(page.getByTestId('passkeys.recovery.done')).toContainText('new recovery code is saved');
  await page.getByTestId('settings.sign-out').click();
  // the old code no longer finds the account; the new one does
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText(
    'No account was found for this recovery code. Check the code.',
  );
  await page.getByTestId('recover.code').fill(fresh);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.passkey-step')).toBeVisible();
});
