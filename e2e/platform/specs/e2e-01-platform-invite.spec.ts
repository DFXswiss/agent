// E2E-01 (07-testing.md section 2.1, 03-api.md sections 4.1-4.3, 04-crypto.md section 5.1): the operator CLI grants
// the platform role `inviter` to a user; that user creates a platform invitation on the platform-invitation screen
// (`/platform/invites`, 06-web.md section 7.4; the role is read from the verified platform log at the next sign-in);
// a new browser opens the link, registers a passkey with
// PRF, confirms the recovery code and lands in its private org. Asserted: private org with default workgroup and
// platform access, genesis verified, suggestion chips shown, the hub holds neither the code nor the recovery code nor
// the name; negative cases from section 2.1. Runs on T1 and T2.
import { expect, test } from '../fixtures/test.ts';
import { addAuthenticator } from '../fixtures/authenticator.ts';
import { anonymous, hubTablesContaining, onboard, onboardWithCode, operator, uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { generateCode, inviteId, inviteSecrets, parseCode } from '../../../web/src/core/crypto/invite';
import { formatTime } from '../../../web/src/core/encoding/time';
import { preparePrivateOrg } from '../../../web/src/core/account/genesis';

test.describe.configure({ mode: 'serial' });

test('@E2E-01 @R-0.1 @R-0.5 @R-1.1 @R-1.2 @R-1.6 @R-1.7 @R-2.1 @R-2.3 @R-P.5 @R-P.12 an inviter’s platform invitation leads a new browser into its own private org', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(240_000);
  // The inviter: a user of the platform (operator invitation), then granted `inviter` by the operator CLI.
  await authenticator('platform');
  const first = await onboard(page, orchestrator, 'Grace Hopper');
  const inviter = await PageUser.open(page, first.recoveryCode);
  // Without the role the hub refuses the inviter's invitation.
  const refusedCode = generateCode();
  const refusedSecrets = await inviteSecrets(refusedCode);
  const refused = {
    invite_id: refusedSecrets.inviteId,
    pk_inv: refusedSecrets.pkInv,
    kind: 'user',
    expires_at: formatTime(Date.now() + 3600_000),
  };
  await expect(inviter.request('POST', '/v1/platform/invites', refused)).rejects.toThrow(/403/);
  expect(await operator(orchestrator, ['grant-role', 'inviter', '--user', inviter.userId])).toContain(
    'appended role_grant',
  );
  await page.goto('/platform/invites');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('shell.platform-invites')).toBeVisible();
  await page.getByTestId('platform-invites.open').click();
  await expect(page.getByTestId('platform-invites.expiry')).toHaveValue('72');
  await page.getByTestId('platform-invites.submit').click();
  const code = (await page.getByTestId('platform-invites.code').textContent())!;
  await expect(page.getByTestId('platform-invites.link')).toHaveText(`${e2e.hubOrigin}/invite#${code}`);
  await page.getByTestId('platform-invites.done').click();
  await expect(page.getByTestId('platform-invites.item').first()).toHaveAttribute('data-state', 'open');
  const id = await inviteId(parseCode(code));

  // The invitee: a new browser profile with a platform authenticator (PRF).
  const context = await browser.newContext({ baseURL: e2e.hubOrigin });
  const invitee = await context.newPage();
  await addAuthenticator(invitee, 'platform');
  const name = 'Katherine Johnson';
  const onboarded = await onboardWithCode(invitee, code, name);
  await expect(invitee.getByTestId('home.screen')).toBeVisible();
  await expect(invitee.locator('[data-testid^="composer.chip."]').first()).toBeVisible();
  expect(await invitee.locator('[data-testid^="composer.chip."]').count()).toBeGreaterThanOrEqual(4);

  // Private org with its default workgroup and platform access; the invitation is used; genesis verifies.
  const [org] = await orchestrator.hubQuery<{ private: boolean; platform_access: boolean; default_wg: string }>(
    `SELECT o.private, o.platform_access, o.default_wg::text FROM hub.org o JOIN hub.wg w ON w.id = o.default_wg
      WHERE o.id = '${uuid(onboarded.org)}' AND w.is_default AND w.org_id = o.id`,
  );
  expect(org).toMatchObject({ private: true, platform_access: true });
  const [used] = await orchestrator.hubQuery<{ used: boolean }>(
    `SELECT used_at IS NOT NULL AS used FROM hub.invite WHERE id = '${id.replace(/[^A-Za-z0-9_-]/g, '')}'`,
  );
  expect(used).toEqual({ used: true });
  const user = await PageUser.open(invitee, onboarded.recoveryCode);
  const log = await user.log(onboarded.org);
  expect(log.state.workgroups.has(org!.default_wg)).toBe(true);
  const [genesis] = await orchestrator.hubQuery<{ seq: number; n: number }>(
    `SELECT min(seq) AS seq, count(*)::int AS n FROM hub.membership_entry WHERE org_id = '${uuid(onboarded.org)}'`,
  );
  expect(genesis).toEqual({ seq: 0, n: 1 });

  // Zero knowledge: neither code nor recovery code nor the display name is stored at the hub.
  const recovery = onboarded.recoveryCode.replace(/\s+/g, '');
  expect(
    await hubTablesContaining(orchestrator, [code, code.replace(/-/g, ''), onboarded.recoveryCode, recovery, name]),
  ).toEqual([]);

  // Negative: registration needs an open invitation – none, one the platform never issued, the used one.
  const none = await anonymous(invitee, 'POST', '/v1/passkeys/register/begin', {});
  expect(none).toMatchObject({ status: 400, body: { error: 'request_invalid' } });
  const neverIssued = await anonymous(invitee, 'POST', '/v1/passkeys/register/begin', {
    invite_id: await inviteId(generateCode()),
  });
  expect(neverIssued).toMatchObject({ status: 404, body: { error: 'invite_unknown' } });
  const consumed = await anonymous(invitee, 'POST', '/v1/passkeys/register/begin', { invite_id: id });
  expect(consumed).toMatchObject({ status: 404, body: { error: 'invite_unknown' } });
  // Negative: a second private org needs a fresh platform invitation (none, or the consumed one).
  for (const platformInviteId of [undefined, id]) {
    const prepared = await preparePrivateOrg({
      userId: user.userId,
      identity: user.identity,
      displayName: name,
      locale: 'en',
      platformInviteId: platformInviteId ?? '',
    });
    const body: Record<string, unknown> = { ...prepared.body };
    if (platformInviteId === undefined) delete body.platform_invite_id;
    await expect(user.request('POST', '/v1/orgs', body)).rejects.toThrow(/403 .*platform_invite_required/);
  }
  await context.close();
});
