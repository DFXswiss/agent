// The recovery code at registration (06-web.md section 3.2): copy, download as text file, confirmation of the last
// group, and "Skip" only behind the consequence dialog, which leaves a persistent reminder.
import { readFileSync } from 'node:fs';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';
import { platformInvite } from './helpers/platform.ts';

async function toRecoveryCode(page: import('@playwright/test').Page, origin: string): Promise<string> {
  const invite = await platformInvite(origin);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill('Ada');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  return (await page.getByTestId('recovery-code.code').textContent())!;
}

test('the code can be copied and downloaded as a text file', async ({ page, context, baseURL }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await addAuthenticator(page);
  const code = await toRecoveryCode(page, baseURL!);
  await page.getByTestId('recovery-code.copy').click();
  await expect(page.getByTestId('recovery-code.copied')).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(code);
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByTestId('recovery-code.download').click(),
  ]);
  expect(download.suggestedFilename()).toBe('agent-recovery-code.txt');
  expect(readFileSync((await download.path())!, 'utf8')).toContain(code);
});

test('a refused clipboard asks to copy by hand', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    });
  });
  await toRecoveryCode(page, baseURL!);
  await page.getByTestId('recovery-code.copy').click();
  await expect(page.getByTestId('recovery-code.copy-failed')).toBeVisible();
});

test('skipping needs the consequence dialog and leaves a reminder', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await toRecoveryCode(page, baseURL!);
  await page.getByTestId('recovery-code.skip').click();
  await expect(page.getByTestId('recovery-code.skip-dialog')).toContainText('all its data would be lost');
  await expect(page.getByTestId('recovery-code.skip-confirm')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('recovery-code.skip-dialog')).toHaveCount(0);
  await page.getByTestId('recovery-code.skip').click();
  await page.getByTestId('recovery-code.skip-cancel').click();
  await expect(page.getByTestId('recovery-code.skip-dialog')).toHaveCount(0);
  await page.getByTestId('recovery-code.skip').click();
  await page.getByTestId('recovery-code.skip-confirm').click();
  await expect(page).toHaveURL(/\/o\/[0-9a-f-]{36}$/);
  await expect(page.getByTestId('shell.recovery-banner')).toBeVisible();
  // the reminder survives a reload and a new sign-in
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('shell.recovery-banner')).toBeVisible();
  await page.getByTestId('shell.recovery-banner.settings').click();
  await expect(page.getByTestId('settings.page')).toBeVisible();
});

test('a confirmed code leaves no reminder', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await expect(page.getByTestId('shell.recovery-banner')).toHaveCount(0);
});

test('a new recovery code from the settings replaces the old one (KOR-01)', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('passkeys.recovery-new.start').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  expect(fresh).not.toBe(done.recoveryCode);
  await expect(page.getByTestId('recovery-code.skip')).toHaveCount(0);
  await page.getByTestId('recovery-code.confirm').fill(fresh.slice(-6));
  await page.getByTestId('recovery-code.continue').click();
  await expect(page.getByTestId('passkeys.recovery-new.notice')).toContainText('new recovery code is saved');
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
