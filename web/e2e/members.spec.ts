// Members and rights (06-web.md section 7.1) against the real hub: an admin invites (link and code shown once, open
// invitations revoked), a second user joins with the code (API, the screen is WEB-08), the admin changes the role,
// grants workgroup access with history, removes the member with the rotation progress; a plain member sees no admin
// controls, leaves, and the admin's browser rotates what the member could read automatically with a toast.
import type { BrowserContext, Page } from '@playwright/test';
import { ORG, WG } from '../src/core/log/state';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { joinWithCode } from './helpers/members.ts';
import { onboard, type Onboarded } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

interface Person {
  readonly page: Page;
  readonly onboarded: Onboarded;
  readonly user: PageUser;
}

async function person(context: BrowserContext, origin: string, name: string): Promise<Person> {
  const page = await context.newPage();
  await addAuthenticator(page);
  const onboarded = await onboard(page, { origin, name });
  return { page, onboarded, user: await PageUser.open(page, onboarded.recoveryCode) };
}

/** Opens `path` with a reload: the identity is locked again and unlocks with the passkey (06-web.md section 2.3). */
export async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

/** Client-side navigation to `path` (the router follows `popstate`; a reload would lock the identity). */
async function navigateInApp(page: Page, path: string): Promise<void> {
  await page.evaluate((target) => {
    history.pushState({}, '', target);
    dispatchEvent(new PopStateEvent('popstate'));
  }, path);
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

/** Ada invites through the screen; returns the code shown once. */
async function invite(page: Page, org: string, role: 'member' | 'admin' = 'member'): Promise<string> {
  if (!page.url().endsWith(`/o/${org}/members`)) await page.getByTestId('shell.nav.members').click();
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.role').selectOption(role);
  await expect(page.getByTestId('members.invite.expiry')).toHaveValue('72');
  await page.getByTestId('members.invite.submit').click();
  await expect(page.getByTestId('members.invite.warning')).toBeVisible();
  const code = (await page.getByTestId('members.invite.code').textContent())!;
  await expect(page.getByTestId('members.invite.link')).toHaveText(`${new URL(page.url()).origin}/invite#${code}`);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(page.url()).origin });
  await page.getByTestId('members.invite.copy-link').click();
  await expect(page.getByTestId('members.invite.copy-link')).toHaveText('Copied');
  await page.getByTestId('members.invite.copy-code').click();
  await expect(page.getByTestId('members.invite.copy-code')).toHaveText('Copied');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(code);
  await page.getByTestId('members.invite.done').click();
  await expect(page.getByTestId('members.invite.result')).toHaveCount(0);
  return code;
}

const member = (page: Page, name: string) => page.getByTestId('members.item').filter({ hasText: name });

test('an admin creates an invitation shown once and revokes it @R-1.4 @R-1.11', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  const ada = await onboard(page, { origin: baseURL!, name: 'Ada Admin' });
  await page.getByTestId('shell.nav.members').click();
  await expect(page.getByTestId('members.screen')).toBeVisible();
  await expect(member(page, 'Ada Admin').getByTestId('members.item.role')).toHaveText('Admin');
  // the only admin: no role control and no leave
  await expect(page.getByTestId('members.item.role-change')).toHaveCount(0);
  await expect(page.getByTestId('members.leave')).toHaveCount(0);
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.cancel').click();
  await invite(page, ada.org, 'admin');
  await expect(page.getByTestId('members.invites.item')).toHaveCount(1);
  await page.getByTestId('members.invites.revoke').click();
  await expect(page.getByTestId('members.invites.empty')).toBeVisible();
  const log = await (await PageUser.open(page, ada.recoveryCode)).log(ada.org);
  const types = log.entries.map((entry) => entry.body.type);
  expect(types.slice(-2)).toEqual(['invite', 'invite_revoke']);
});

test('a member joins; the admin changes the role, grants with history and removes with rotation @R-8.1 @R-8.2 @R-5.4', async ({
  page,
  baseURL,
  newContext,
}) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin, name: 'Ada Admin' });
  const adaUser = await PageUser.open(page, ada.recoveryCode);
  const wg = await adaUser.createWorkgroup(ada.org, 'Research');
  const code = await invite(page, ada.org);
  const bob = await person(await newContext(), origin, 'Bob Member');
  expect(await joinWithCode(bob.user, origin, code, 'Bob Member')).toBe(ada.org);
  // the admin's screen follows the log and the profile live
  const row = member(page, 'Bob Member');
  await expect(row.getByTestId('members.item.role')).toHaveText('Member');
  await row.getByTestId('members.item.role-change').click();
  await page.getByTestId('members.role.confirm').click();
  await expect(row.getByTestId('members.item.role')).toHaveText('Admin');
  await row.getByTestId('members.item.role-change').click();
  await page.getByTestId('members.role.confirm').click();
  await expect(row.getByTestId('members.item.role')).toHaveText('Member');
  // grant with history
  await row.getByTestId('members.item.grant').click();
  await page.getByTestId('members.grant.access').selectOption('write');
  await page.getByTestId('members.grant.history').check();
  await page.getByTestId('members.grant.confirm').click();
  await expect(row.getByTestId('members.item.access')).toContainText('Research');
  // ungrant through the access chip: the workgroup is rotated
  await row.getByTestId('members.item.ungrant').click();
  await page.getByTestId('members.ungrant.confirm').click();
  await expect(page.getByTestId('members.ungrant')).toHaveCount(0);
  await expect(row.getByTestId('members.item.access')).not.toContainText('Research');
  expect((await adaUser.log(ada.org)).state.epoch(WG, wg)).toBe(2);
  // remove: the dialog explains, then shows the rotation progress (the append is held back briefly to watch it)
  await row.getByTestId('members.item.remove').click();
  await expect(page.getByTestId('members.remove')).toContainText('already read');
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/v1/orgs/${ada.org}/log`, async (route) => {
    if (route.request().method() === 'POST') await held;
    await route.continue();
  });
  await page.getByTestId('members.remove.confirm').click();
  await expect(page.getByTestId('members.remove.progress.rotate')).toHaveText('Rotating keys 2/2');
  await expect(page.getByTestId('members.remove.progress.wrap')).toHaveText('Wrapping for 1 recipients');
  await expect(page.getByTestId('members.remove.progress.send')).toBeVisible();
  release();
  await expect(page.getByTestId('members.remove')).toHaveCount(0);
  await expect(member(page, 'Bob Member')).toHaveCount(0);
  const log = await adaUser.log(ada.org);
  // bob no longer read Research after the ungrant: R is the org and the default workgroup
  const tail = log.entries.slice(-3).map((entry) => entry.body.type);
  expect(tail).toEqual(['remove', 'rotate', 'rotate']);
  expect(log.state.epoch(ORG, ada.org)).toBe(2);
  expect(log.state.epoch(WG, wg)).toBe(2);
  expect(log.state.epoch(WG, log.state.defaultWg)).toBe(2);
  // the hub refuses the removed member at once
  await expect(bob.user.request('GET', `/v1/orgs/${ada.org}`)).rejects.toThrow(/403|404/);
});

test('a plain member sees no admin controls and leaves; the admin rotates automatically @R-8.3 @R-8.6', async ({
  page,
  baseURL,
  newContext,
}) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin, name: 'Ada Admin' });
  const adaUser = await PageUser.open(page, ada.recoveryCode);
  const code = await invite(page, ada.org);
  const bob = await person(await newContext(), origin, 'Bob Reader');
  await joinWithCode(bob.user, origin, code, 'Bob Reader');
  await visit(bob.page, `/o/${ada.org}/members`);
  await expect(member(bob.page, 'Bob Reader').getByTestId('members.item.role')).toHaveText('Member');
  for (const id of [
    'members.invite.open',
    'members.invites',
    'members.item.role-change',
    'members.item.grant',
    'members.item.remove',
    'members.item.ungrant',
  ]) {
    await expect(bob.page.getByTestId(id)).toHaveCount(0);
  }
  await bob.page.getByTestId('shell.nav.workgroups').click();
  await expect(bob.page.getByTestId('workgroups.item')).toHaveCount(1);
  for (const id of ['workgroups.create.open', 'workgroups.item.rename', 'workgroups.item.archive']) {
    await expect(bob.page.getByTestId(id)).toHaveCount(0);
  }
  // leave
  await bob.page.getByTestId('shell.nav.members').click();
  await bob.page.getByTestId('members.leave').click();
  await bob.page.getByTestId('members.leave-dialog.confirm').click();
  await expect(bob.page).toHaveURL(new RegExp(`/o/${bob.onboarded.org}$`));
  // the left org is "not found" for Bob now; an org this browser never verified stays "loading"
  await navigateInApp(bob.page, `/o/${ada.org}/members`);
  await expect(bob.page.getByTestId('members.not-found')).toBeVisible();
  await navigateInApp(bob.page, `/o/${ada.org}/workgroups`);
  await expect(bob.page.getByTestId('workgroups.not-found')).toBeVisible();
  const unknown = '0190f000-0000-7000-8000-00000000abcd';
  await navigateInApp(bob.page, `/o/${unknown}/members`);
  await expect(bob.page.getByTestId('members.loading')).toBeVisible();
  await navigateInApp(bob.page, `/o/${unknown}/workgroups`);
  await expect(bob.page.getByTestId('workgroups.loading')).toBeVisible();
  // the admin's browser owes the rotation and sends it with a toast
  await expect(page.getByTestId('rotation-toast.item')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('rotation-toast.close').click();
  await expect(page.getByTestId('rotation-toast.item')).toHaveCount(0);
  const log = await adaUser.log(ada.org);
  expect(log.state.pendingScopes()).toEqual([]);
  expect(log.state.epoch(ORG, ada.org)).toBe(2);
});
