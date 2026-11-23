// E2E-30 (07-testing.md sections 1, 2.1; 04-crypto.md sections 5.4–5.6; 06-web.md sections 3.3, 3.5, 3.6): one account
// with three kinds of passkeys and the recovery code. A second passkey on a roaming authenticator (`transport: usb`)
// signs in and unlocks alone; a passkey of an authenticator without PRF is not accepted (KOR-22), and an existing
// passkey signing in from an authenticator without PRF takes the `prf_missing` path (unlock with the recovery code);
// after "losing" every passkey the recovery code registers a new one and issues a new
// recovery code that replaces the old one (slot and code). Each way unlocks the same identity: after every sign-in the
// app itself decrypts a whiteboard entry written before with the org's keys (wrapped only for this identity), the
// user ID stays the same, and the identity of the recovery slot opens the same keys. Runs on T1.
import { expect, test } from '../fixtures/test.ts';
import { addAuthenticator, type VirtualAuthenticator } from '../fixtures/authenticator.ts';
import { anonymousBudget, closingChecks, COST, navigate, personOf, signIn } from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import type { Page } from '@playwright/test';

const SECRET = 'Only for my identity kz30-entry-g5h7j9';

async function signOut(page: Page): Promise<void> {
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await expect(page).toHaveURL(/\/login$/);
}

async function lose(authenticator: VirtualAuthenticator): Promise<void> {
  await authenticator.cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: authenticator.id });
}

/** Adds a passkey in /settings: "on another device" (roaming authenticator) or "on this device" (platform). */
async function addPasskey(page: Page, where: 'other-device' | 'this-device', notice: string, count: number) {
  await page.getByTestId('shell.settings').click();
  await page.getByTestId(`passkeys.add.${where}`).click();
  await expect(page.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'confirm');
  await page.getByTestId('passkeys.add.confirm').click();
  await expect(page.getByTestId('passkeys.add.notice')).toHaveText(notice);
  await expect(page.getByTestId('passkeys.item')).toHaveCount(count);
}

test('@E2E-30 @R-1.6 @R-1.7 @R-5.6 @R-14.1 @R-P.5 a second passkey, a passkey signing in without PRF and the recovery code unlock the same identity', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-30 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(600_000);
  const first = await authenticator('platform');
  const ada = await personOf(page, first, orchestrator, 'Ada Passkeys');
  const org = ada.onboarded.org;
  const identity = ada.user.identity.public();
  // Content only this identity can read: a whiteboard entry of the default workgroup (sealed with its key).
  const wg = (await ada.user.log(org)).state.defaultWg;
  await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(SECRET);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: SECRET })).toBeVisible({ timeout: 30_000 });
  const sameIdentity = async (recoveryCode: string) => {
    // the app's own unlock (passkey PRF slot, or the recovery code on the prf_missing page) opens the entry
    await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
    await expect(page.getByTestId('whiteboard.item').filter({ hasText: SECRET })).toBeVisible({ timeout: 30_000 });
    const me = (await ada.user.request('GET', '/v1/me')) as { user_id: string };
    expect(me.user_id).toBe(ada.user.userId);
    const user = await PageUser.open(page, recoveryCode);
    expect(user.userId).toBe(ada.user.userId);
    expect(user.identity.public()).toEqual(identity);
    // the org's keys open with this unlock too (wraps for the identity, 04-crypto.md section 6)
    const log = await user.log(org);
    expect((await user.keys(org, log)).size).toBeGreaterThanOrEqual(2);
  };

  // 1. A second passkey on a roaming authenticator; alone it signs in and unlocks (PRF).
  const usb = await addAuthenticator(page, 'usb');
  await addPasskey(page, 'other-device', 'The passkey was added. It signs in and unlocks your keys.', 2);
  await expect(page.getByTestId('passkeys.item').and(page.locator('[data-prf="true"]'))).toHaveCount(2);
  expect(await usb.credentials()).toHaveLength(1);
  await signOut(page);
  await lose(first);
  await signIn(page);
  await expect(page).toHaveURL(new RegExp(`/o/${uuid(org)}$`), { timeout: 30_000 });
  await expect(page.getByTestId('prf-missing.page')).toHaveCount(0);
  await sameIdentity(ada.onboarded.recoveryCode);

  // 2. A passkey without PRF is not accepted (KOR-22): it is removed again and the account keeps its two passkeys.
  // (Chromium's virtual authenticators ignore the attachment hint: the new one is the only one present.)
  const { credentials } = await usb.cdp.send('WebAuthn.getCredentials', { authenticatorId: usb.id });
  await lose(usb);
  const noPrf = await addAuthenticator(page, 'no-prf');
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('passkeys.add.this-device').click();
  await page.getByTestId('passkeys.add.confirm').click();
  await expect(page.getByTestId('prf-required')).toContainText('The passkey was not added.');
  await expect(page.getByTestId('passkeys.item')).toHaveCount(2);
  // The usb passkey signs in from an authenticator without PRF: the unlock needs the recovery code (`prf_missing`).
  await noPrf.cdp.send('WebAuthn.clearCredentials', { authenticatorId: noPrf.id });
  for (const credential of credentials) {
    await noPrf.cdp.send('WebAuthn.addCredential', { authenticatorId: noPrf.id, credential });
  }
  await signOut(page);
  await signIn(page);
  await expect(page.getByTestId('prf-missing.explain')).toHaveText(
    'This passkey cannot open your data on this device.',
  );
  await page.getByTestId('prf-missing.recovery.input').fill('AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA');
  await anonymousBudget(COST.lookup);
  await page.getByTestId('prf-missing.recovery.submit').click();
  await expect(page.getByTestId('prf-missing.error')).toBeVisible();
  await page.getByTestId('prf-missing.recovery.input').fill(ada.onboarded.recoveryCode);
  await page.getByTestId('prf-missing.recovery.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${uuid(org)}$`), { timeout: 30_000 });
  await sameIdentity(ada.onboarded.recoveryCode);

  // 3. Every passkey lost: the recovery code registers a new passkey and issues a new recovery code.
  const [before] = await orchestrator.hubQuery<{ recovery_id: string }>(
    `SELECT recovery_id FROM hub.user_identity WHERE user_id = '${uuid(ada.user.userId)}'`,
  );
  await signOut(page);
  await lose(noPrf);
  const replacement = await addAuthenticator(page, 'platform');
  await anonymousBudget(COST.recover);
  await page.getByTestId('login.recover').click();
  await expect(page).toHaveURL(/\/recover$/);
  await page.getByTestId('recover.code').fill(ada.onboarded.recoveryCode.toLowerCase());
  await page.getByTestId('recover.submit').click();
  await page.getByTestId('recover.create-passkey').click();
  await page.getByTestId('recover.unlock').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  expect(fresh).not.toBe(ada.onboarded.recoveryCode);
  await page.getByTestId('recovery-code.continue').click();
  await expect(page).toHaveURL(new RegExp(`/o/${uuid(org)}$`), { timeout: 30_000 });
  expect(await replacement.credentials()).toHaveLength(1);
  await sameIdentity(fresh);
  await expect(PageUser.open(page, ada.onboarded.recoveryCode)).rejects.toThrow();
  const [after] = await orchestrator.hubQuery<{ recovery_id: string }>(
    `SELECT recovery_id FROM hub.user_identity WHERE user_id = '${uuid(ada.user.userId)}'`,
  );
  expect(after!.recovery_id).not.toBe(before!.recovery_id);
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.item')).toHaveCount(4);

  // The old code is gone, the new one is the recovery code now; the new passkey signs in and unlocks with PRF.
  await signOut(page);
  await anonymousBudget(COST.page + 2 * COST.lookup);
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(ada.onboarded.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText(
    'No account was found for this recovery code. Check the code.',
  );
  await page.getByTestId('recover.code').fill(fresh);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.passkey-step')).toBeVisible();
  await anonymousBudget(COST.page);
  await page.goto('/login');
  await signIn(page);
  await expect(page).toHaveURL(new RegExp(`/o/${uuid(org)}$`), { timeout: 30_000 });
  await expect(page.getByTestId('prf-missing.page')).toHaveCount(0);
  await sameIdentity(fresh);
  await expectNoPlaintext(orchestrator, ['kz30-entry-g5h7j9']);
  await closingChecks(orchestrator);
});
