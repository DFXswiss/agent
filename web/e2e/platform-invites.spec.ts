// Platform invitations (06-web.md section 7.4, ADR-026; E2E-01 web part) against the real hub: the route and the
// header link exist only for users whose verified platform log grants `inviter`; an inviter creates an invitation
// (code drawn in the browser, never sent), sees link and code once, a new browser registers with the link and lands in
// its private org, the list shows the invitation as used; a second invitation is revoked and refused afterwards.
import type { Page } from '@playwright/test';
import { inviteId, parseCode } from '../src/core/crypto/invite';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard, ORG_PATH } from './helpers/onboard.ts';
import { appendPlatform } from './helpers/platform.ts';
import { PageUser } from './helpers/user.ts';

/** Opens `path` with a reload (the identity is locked again and unlocks with the passkey). */
async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

/** The row of one invitation (the hub lists the invitations of every inviter, other specs run in parallel). */
const row = async (page: Page, code: string) =>
  page.locator(`[data-testid="platform-invites.item"][data-invite="${await inviteId(parseCode(code))}"]`);

async function createInvitation(page: Page, hours?: string): Promise<{ code: string; link: string }> {
  await page.getByTestId('platform-invites.open').click();
  await expect(page.getByTestId('platform-invites.expiry')).toHaveValue('72');
  if (hours !== undefined) await page.getByTestId('platform-invites.expiry').selectOption(hours);
  await page.getByTestId('platform-invites.submit').click();
  await expect(page.getByTestId('platform-invites.warning')).toBeVisible();
  const code = (await page.getByTestId('platform-invites.code').textContent())!;
  const link = (await page.getByTestId('platform-invites.link').textContent())!;
  expect(link).toBe(`${new URL(page.url()).origin}/invite#${code}`);
  return { code, link };
}

test('an inviter creates a platform invitation that brings a new customer into a private org @R-1.2 @R-P.12', async ({
  page,
  baseURL,
  newContext,
}) => {
  await addAuthenticator(page);
  const grace = await onboard(page, { origin: baseURL!, name: 'Grace Hopper' });
  const user = await PageUser.open(page, grace.recoveryCode);
  // without the role: no link, the route is not available, the hub refuses
  await expect(page.getByTestId('shell.settings')).toBeVisible();
  await expect(page.getByTestId('shell.platform-invites')).toHaveCount(0);
  await visit(page, '/platform/invites');
  await expect(page.getByTestId('platform-invites.forbidden')).toBeVisible();
  await expect(user.request('GET', '/v1/platform/invites')).rejects.toThrow(/403/);

  // the operator grants `inviter`; the next sign-in verifies it from the platform log
  await appendPlatform(baseURL!, [{ type: 'role_grant', subject: user.userId, role: 'inviter' }]);
  await visit(page, '/platform/invites');
  await expect(page.getByTestId('shell.platform-invites')).toBeVisible();
  await expect(page.getByTestId('platform-invites.screen')).toBeVisible();
  const sent: string[] = [];
  page.on('request', (request) => sent.push(`${request.url()}\n${request.postData() ?? ''}`));
  const invitation = await createInvitation(page);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL! });
  await page.getByTestId('platform-invites.copy-link').click();
  await expect(page.getByTestId('platform-invites.copy-link')).toHaveText('Copied');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(invitation.link);
  await page.getByTestId('platform-invites.done').click();
  await expect(page.getByTestId('platform-invites.code')).toHaveCount(0);
  const first = await row(page, invitation.code);
  await expect(first).toHaveAttribute('data-state', 'open');
  expect(sent.join('\n')).not.toContain(invitation.code);
  expect(sent.join('\n')).not.toContain(invitation.code.replace(/-/g, ''));

  // a new browser registers with the link and lands in its private org
  const context = await newContext();
  const invitee = await context.newPage();
  await addAuthenticator(invitee);
  await invitee.goto(new URL(invitation.link).pathname + new URL(invitation.link).hash);
  await invitee.getByTestId('invite.display-name').fill('Katherine Johnson');
  await invitee.getByTestId('invite.create-passkey').click();
  await invitee.getByTestId('invite.unlock').click();
  await expect(invitee).toHaveURL(ORG_PATH);
  const org = ORG_PATH.exec(invitee.url())![1]!;
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(await row(page, invitation.code)).toHaveAttribute('data-state', 'used');
  expect(org).toMatch(/^[0-9a-f-]{36}$/);

  // a second invitation is revoked; its link is refused
  const second = await createInvitation(page, '24');
  await page.getByTestId('platform-invites.done').click();
  const open = await row(page, second.code);
  await expect(open).toHaveAttribute('data-state', 'open');
  await open.getByTestId('platform-invites.item.revoke').click();
  await expect(open).toHaveAttribute('data-state', 'revoked');
  const late = await context.newPage();
  await late.goto(new URL(second.link).pathname + new URL(second.link).hash);
  await expect(late.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
});
