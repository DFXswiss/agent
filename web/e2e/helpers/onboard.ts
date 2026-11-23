// The platform-invitation onboarding through the UI (06-web.md sections 3.1, 3.1.1, 3.2), shared by the specs:
// invitation link → display name → gesture 1 → gesture 2 → recovery code (confirmed or skipped) → without PRF the
// "Remember this browser" offer (declined unless `remember`) → private org.
import { expect, type Page } from '@playwright/test';
import { platformInvite } from './platform';

export interface Onboarded {
  readonly code: string;
  readonly inviteId: string;
  readonly recoveryCode: string;
  readonly org: string;
}

export const ORG_PATH = /\/o\/([0-9a-f-]{36})$/;

export async function onboard(
  page: Page,
  options: { readonly name?: string; readonly skip?: boolean; readonly remember?: boolean; readonly origin: string },
): Promise<Onboarded> {
  const invite = await platformInvite(options.origin);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill(options.name ?? 'Ada Lovelace');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  const recoveryCode = (await page.getByTestId('recovery-code.code').textContent())!;
  if (options.skip) {
    await page.getByTestId('recovery-code.skip').click();
    await page.getByTestId('recovery-code.skip-confirm').click();
  } else {
    await page.getByTestId('recovery-code.confirm').fill(recoveryCode.slice(-6).toLowerCase());
    await page.getByTestId('recovery-code.continue').click();
  }
  // an authenticator without PRF gets the "Remember this browser" offer first
  const offer = page.getByTestId('prf-missing.offer');
  if (await Promise.race([offer.waitFor().then(() => true), page.waitForURL(ORG_PATH).then(() => false)])) {
    await page.getByTestId(options.remember ? 'prf-missing.offer.remember' : 'prf-missing.offer.skip').click();
  }
  await expect(page).toHaveURL(ORG_PATH);
  const org = ORG_PATH.exec(page.url())![1]!;
  return { ...invite, recoveryCode, org };
}
