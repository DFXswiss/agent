// The platform-invitation onboarding through the UI (06-web.md sections 3.1, 3.1.1, 3.2; KOR-22), shared by the specs:
// invitation link → display name → gesture 1 → gesture 2 → private org. A passkey without PRF is refused
// ("prf-required"); with `retry` the spec gets the chance to switch authenticators and the same link is used again.
// Specs that open the identity in Node need a recovery code: a registration makes none, so one is created in /settings
// (one passkey gesture) unless `code: false` (then `recoveryCode` is empty).
import { expect, type Page } from '@playwright/test';
import { platformInvite } from './platform';

export interface Onboarded {
  readonly code: string;
  readonly inviteId: string;
  /** The recovery code ('' with `code: false`). */
  readonly recoveryCode: string;
  readonly org: string;
}

export const ORG_PATH = /\/o\/([0-9a-f-]{36})$/;

/** Client-side navigation (the router follows `popstate`; a reload would lock the identity), on every layout. */
export async function navigateInApp(page: Page, path: string): Promise<void> {
  await page.evaluate((target) => {
    history.pushState({}, '', target);
    dispatchEvent(new PopStateEvent('popstate'));
  }, path);
}

/** "Create recovery code" in /settings: the code shown, "Done", the passkey gesture; then back where the page was. */
export async function createRecoveryCode(page: Page): Promise<string> {
  const back = new URL(page.url()).pathname;
  await navigateInApp(page, '/settings');
  await page.getByTestId('passkeys.recovery.create').click();
  const code = (await page.getByTestId('recovery-code.code').textContent())!;
  await page.getByTestId('recovery-code.continue').click();
  await expect(page.getByTestId('passkeys.recovery.done')).toBeVisible();
  await navigateInApp(page, back);
  await expect(page).toHaveURL(new RegExp(`${back}$`));
  return code;
}

export async function onboard(
  page: Page,
  options: {
    readonly name?: string;
    readonly code?: boolean;
    readonly origin: string;
    /** Called when the passkey was refused for lack of PRF; the helper then tries again with the same link. */
    readonly retry?: () => Promise<void>;
  },
): Promise<Onboarded> {
  const invite = await platformInvite(options.origin);
  await page.goto(`/invite#${invite.code}`);
  await page.getByTestId('invite.display-name').fill(options.name ?? 'Ada Lovelace');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  if (options.retry !== undefined) {
    await expect(page.getByTestId('prf-required')).toBeVisible();
    await options.retry();
    await page.getByTestId('prf-required.retry').click();
    await page.getByTestId('invite.create-passkey').click();
    await page.getByTestId('invite.unlock').click();
  }
  await expect(page).toHaveURL(ORG_PATH);
  const org = ORG_PATH.exec(page.url())![1]!;
  const recoveryCode = options.code === false ? '' : await createRecoveryCode(page);
  return { ...invite, recoveryCode, org };
}
