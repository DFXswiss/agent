// Steps of the whiteboard and notification scenarios (SCN-06, E2E-25/26): the admin invites members on the member
// screen (06-web.md section 4.2, role and workgroup access chosen there), each member registers in a browser profile
// of its own through the invitation link (06-web.md section 3.1, a new user with passkey and PRF), and the admin's
// whiteboard editor (06-web.md section 6.1).
import { type Browser, expect, type Page } from '@playwright/test';
import { addAuthenticator } from './authenticator.ts';
import type { E2eEnv } from './env.ts';
import { ONBOARD_SPACING_MS } from './platform.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

export interface Member {
  readonly page: Page;
  readonly user: PageUser;
  readonly name: string;
  readonly recoveryCode: string;
}

/** The admin's invitation on the member screen: `access` per workgroup (none for the others); returns the code. */
export async function inviteMember(
  admin: Page,
  org: string,
  access: Readonly<Record<string, 'read' | 'write'>>,
  role: 'member' | 'admin' = 'member',
): Promise<string> {
  await navigateInApp(admin, `/o/${org}/members`);
  await admin.getByTestId('members.invite.open').click();
  await admin.getByTestId('members.invite.role').selectOption(role);
  for (const [wg, right] of Object.entries(access))
    await admin.getByTestId(`members.invite.wg.${wg}`).selectOption(right);
  await admin.getByTestId('members.invite.submit').click();
  const code = (await admin.getByTestId('members.invite.code').textContent())!;
  await admin.getByTestId('members.invite.done').click();
  return code;
}

/**
 * A new user registers with the org invitation `code` in a new browser profile with a platform authenticator (PRF):
 * display name, passkey, unlock, recovery code confirmed; lands on the org. Registrations are anonymous requests like
 * onboardings and keep the same spacing (`ONBOARD_SPACING_MS`, the hub allows 30 per minute and source).
 */
export async function joinAsNewUser(
  browser: Browser,
  e2e: E2eEnv,
  org: string,
  code: string,
  name: string,
): Promise<Member> {
  await new Promise((resolve) => setTimeout(resolve, ONBOARD_SPACING_MS));
  const context = await browser.newContext({ baseURL: e2e.hubOrigin });
  const page = await context.newPage();
  await addAuthenticator(page, 'platform');
  await page.goto(`/invite#${code}`);
  await page.getByTestId('invite.display-name').fill(name);
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  const recoveryCode = (await page.getByTestId('recovery-code.code').textContent())!;
  await page.getByTestId('recovery-code.confirm').fill(recoveryCode.slice(-6).toLowerCase());
  await page.getByTestId('recovery-code.continue').click();
  await expect(page).toHaveURL(new RegExp(`/o/${org}$`), { timeout: 30_000 });
  return { page, name, recoveryCode, user: await PageUser.open(page, recoveryCode) };
}

/** Writes a new entry on the board at `path` through the editor, optionally with "Notify participants". */
export async function writeEntry(page: Page, path: string, text: string, notify = false): Promise<void> {
  await navigateInApp(page, path);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(text);
  if (notify) await page.getByTestId('whiteboard.editor.notify').check();
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: text })).toBeVisible();
}
