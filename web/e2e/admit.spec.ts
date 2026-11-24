// Direct admission of an existing account (06-web.md section 7.1 "Add member", ADR-060) against the real hub: the
// operator certifies both identities in the platform log (`user_certify`), the person copies its account code from
// the settings, the admin types it on the member screen (a malformed code is refused first), sees the verified
// account and admits it with one button; the same code again is "a member already". The log then holds `admit` with
// the new member not yet consented.
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { navigateInApp, onboard } from './helpers/onboard.ts';
import { appendPlatform } from './helpers/platform.ts';
import { PageUser } from './helpers/user.ts';

/** Opens `path` with a reload (the platform log is read again); the identity unlocks with the passkey. */
async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

async function find(page: Page, code: string): Promise<void> {
  await page.getByTestId('members.admit.code').fill(code);
  await page.getByTestId('members.admit.find').click();
}

test('an admin adds an existing account by its account code without the person confirming', async ({
  page,
  baseURL,
  newContext,
}) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin, name: 'Ada Admin' });
  const adaUser = await PageUser.open(page, ada.recoveryCode);
  const bobPage = await (await newContext()).newPage();
  await addAuthenticator(bobPage);
  const bob = await onboard(bobPage, { origin, name: 'Bob Existing' });
  const bobUser = await PageUser.open(bobPage, bob.recoveryCode);
  await appendPlatform(origin, [
    { type: 'user_certify', user: adaUser.userId, pub: adaUser.identity.public() },
    { type: 'user_certify', user: bobUser.userId, pub: bobUser.identity.public() },
  ]);
  // the person's account code in the settings, copied
  await bobPage.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
  await navigateInApp(bobPage, '/settings');
  const value = bobPage.getByTestId('passkeys.account-code.value');
  await expect(value).toHaveText(/^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$/);
  const code = (await value.textContent())!;
  await expect(bobPage.getByTestId('passkeys.account-code.copy')).toHaveText('Copy code');
  await bobPage.getByTestId('passkeys.account-code.copy').click();
  await expect(bobPage.getByTestId('passkeys.account-code.copy')).toHaveText('Copied');
  expect(await bobPage.evaluate(() => navigator.clipboard.readText())).toBe(code);
  // the admin types it (after a malformed one) and admits
  await visit(page, `/o/${ada.org}/members`);
  await page.getByTestId('members.admit.open').click();
  await find(page, 'nope');
  await expect(page.getByTestId('members.admit.error')).toHaveText('Code not valid');
  await find(page, code);
  await expect(page.getByTestId('members.admit.found')).toHaveText(`Account ${code} found and verified`);
  await expect(page.getByTestId('members.admit.error')).toHaveCount(0);
  await page.getByTestId('members.admit.name').fill('Bob Example');
  await page.getByTestId('members.admit.submit').click();
  await expect(page.getByTestId('members.admit.done')).toContainText(`Bob Example (account ${code}) is a member now.`);
  // the name every member sees at once (ADR-067), marked as given by the admin
  await expect(page.getByTestId('members.item.name').filter({ hasText: 'Bob Example' })).toHaveAttribute(
    'data-source',
    'admin',
  );

  await expect(page.getByTestId('members.item')).toHaveCount(2);
  // the same code again
  await page.getByTestId('members.admit.open').click();
  await expect(page.getByTestId('members.admit.done')).toHaveCount(0);
  await find(page, code);
  await expect(page.getByTestId('members.admit.error')).toHaveText('This account is a member already.');
  await page.getByTestId('members.admit.cancel').click();
  await expect(page.getByTestId('members.admit.form')).toHaveCount(0);
  const log = await adaUser.log(ada.org);
  expect(log.entries.at(-1)!.body).toMatchObject({ type: 'admit', subject: bobUser.userId, role: 'member' });
  expect(log.state.member(bobUser.userId)).toMatchObject({ role: 'member', consented: false });
});
