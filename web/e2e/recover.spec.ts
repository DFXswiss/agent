// Recovery with the recovery code (06-web.md section 3.5, 04-crypto.md section 5.5) against the real hub: after
// "losing" every passkey (the virtual authenticator is removed) the code registers a new passkey, signs in with it and
// issues a new recovery code that replaces the old one; a refused recovery shows the waiting time of the hub's limit
// (20 per day and recovery code); cancelled ceremonies can be repeated; a new passkey without PRF offers "Remember this
// browser".
import { parseRecoveryCode, recoveryId } from '../src/core/crypto/recovery';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator, type VirtualAuthenticator } from './helpers/authenticator.ts';
import { cancelNext } from './helpers/cancel.ts';
import { ORG_PATH, onboard } from './helpers/onboard.ts';

async function lose(authenticator: VirtualAuthenticator): Promise<void> {
  await authenticator.cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: authenticator.id });
}

test('recovery after losing every passkey replaces the passkey and the recovery code', async ({ page, baseURL }) => {
  const lost = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await lose(lost);
  const replacement = await addAuthenticator(page);
  await page.getByTestId('login.recover').click();
  await expect(page).toHaveURL(/\/recover$/);
  // text that is no code at all, then a wrong code (one message for every unknown code)
  await page.getByTestId('recover.code').fill('AAAAA');
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText('Recovery code incomplete');
  // a wrong code is refused with one message for every unknown code
  await page.getByTestId('recover.code').fill('AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA');
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText(
    'No account was found for this recovery code. Check the code.',
  );
  await page.getByTestId('recover.code').fill(done.recoveryCode.toLowerCase());
  await page.getByTestId('recover.submit').click();
  await page.getByTestId('recover.create-passkey').click();
  await page.getByTestId('recover.unlock').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  expect(fresh).not.toBe(done.recoveryCode);
  await expect(page.getByTestId('recovery-code.skip')).toHaveCount(0);
  await page.getByTestId('recovery-code.confirm').fill('WRONG1');
  await page.getByTestId('recovery-code.continue').click();
  await expect(page.getByTestId('recovery-code.mismatch')).toBeVisible();
  await page.getByTestId('recovery-code.confirm').fill(fresh.slice(-6));
  await page.getByTestId('recovery-code.continue').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  expect(await replacement.credentials()).toHaveLength(1);
  // the account has the lost and the new passkey; the new one unlocks with PRF
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.item')).toHaveCount(2);
  await expect(page.getByTestId('passkeys.item.current')).toHaveCount(1);
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  // the old code is gone, the new one is the recovery code now
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText(
    'No account was found for this recovery code. Check the code.',
  );
  await page.getByTestId('recover.code').fill(fresh);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.passkey-step')).toBeVisible();
  await page.goto('/recover');
  await page.getByTestId('recover.login').click();
  await expect(page).toHaveURL(/\/login$/);
});

test('a refused recovery shows the waiting time of the limit per recovery code', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  const id = await recoveryId(parseRecoveryCode(done.recoveryCode));
  // 20 attempts per day and recovery code (03-api.md section 4.1); the hub counts them in its database
  const statuses = await page.evaluate(async (recoveryIdText) => {
    const seen: number[] = [];
    for (let i = 0; i < 20; i += 1) {
      const response = await fetch('/v1/recovery/begin', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ recovery_id: recoveryIdText }),
      });
      seen.push(response.status);
    }
    return seen;
  }, id);
  expect(statuses).toEqual(Array.from({ length: 20 }, () => 200));
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText(
    /^Too many recovery attempts\. Try again in 2[34] hours\.$/,
  );
  await expect(page.getByTestId('recover.code')).toBeVisible();
});

test('cancelled ceremonies of the recovery can be repeated', async ({ page, baseURL }) => {
  const lost = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await lose(lost);
  await addAuthenticator(page);
  await cancelNext(page, 'create');
  await cancelNext(page, 'get');
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await page.getByTestId('recover.create-passkey').click();
  await expect(page.getByTestId('recover.notice')).toHaveText('Passkey creation was cancelled – try again.');
  // the proof was spent: the next attempt begins again with the code
  await page.getByTestId('recover.create-passkey').click();
  await page.getByTestId('recover.unlock').click();
  await expect(page.getByTestId('recover.notice')).toHaveText('Sign-in was cancelled – try again.');
  await page.getByTestId('recover.unlock').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  await page.getByTestId('recovery-code.confirm').fill(fresh.slice(-6));
  await page.getByTestId('recovery-code.continue').click();
  await expect(page).toHaveURL(ORG_PATH);
});

test('a new passkey without PRF offers to remember the browser after the new code', async ({ page, baseURL }) => {
  const lost = await addAuthenticator(page);
  const done = await onboard(page, { origin: baseURL! });
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await lose(lost);
  await addAuthenticator(page, { hasPrf: false });
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await page.getByTestId('recover.create-passkey').click();
  await page.getByTestId('recover.unlock').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  await page.getByTestId('recovery-code.confirm').fill(fresh.slice(-6));
  await page.getByTestId('recovery-code.continue').click();
  await expect(page.getByTestId('prf-missing.offer')).toBeVisible();
  await page.getByTestId('prf-missing.offer.remember').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  // the remembered browser unlocks the next sign-in without the recovery code
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('passkeys.browser.remembered')).toBeVisible();
  await page.getByTestId('settings.sign-out').click();
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  await expect(page.getByTestId('prf-missing.page')).toHaveCount(0);
});
