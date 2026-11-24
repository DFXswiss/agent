// E2E-03 (07-testing.md section 2.1; 04-crypto.md sections 4.7, 5.7; 03-api.md sections 4.2, 12; ADR-005): every
// invitation code is single use, expires and can be revoked. A used code, an expired code (an invitation valid for a
// few seconds: the hub has no fake clock, decision SCN-03 1) and a revoked user and device invitation all end in
// `invite_unknown` – on the invitation screen, at the anonymous lookup and for `agent-runner enroll` – the hub deletes
// their packages (`package_ct` NULL; the expiry job within a minute) and writes the audit rows `invite.create`,
// `invite.register`, `log.accept`, `invite.expire`, `invite.revoke`. Runs on T1.
import { expect, test } from '../fixtures/test.ts';
import {
  addDeviceInvitation,
  anonymousBudget,
  closingChecks,
  COST,
  enrollAttempt,
  openInvite,
  inviteByApi,
  inviteMember,
  joinAsNewUser,
  navigate,
  newBrowser,
  personOf,
  spawnRunner,
  removeRunner,
} from '../fixtures/identity.ts';
import { anonymous, uuid } from '../fixtures/platform.ts';
import { inviteSecrets, parseCode } from '../../../web/src/core/crypto/invite';

const ID = /^[A-Za-z0-9_-]+$/;

async function idOf(code: string): Promise<string> {
  const id = (await inviteSecrets(parseCode(code))).inviteId;
  if (!ID.test(id)) throw new Error(`not an invite ID: ${id}`);
  return id;
}

test('@E2E-03 @R-1.4 @R-1.10 @R-1.11 @R-1.12 @R-P.5 used, expired and revoked invitation codes are unknown and their packages deleted', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-03 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(600_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;
  const stranger = await newBrowser(browser, e2e);
  const unknown = async (code: string) => {
    await openInvite(stranger.page, code);
    await expect(stranger.page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
    await anonymousBudget(COST.lookup);
    const lookup = await anonymous(stranger.page, 'POST', '/v1/invites/lookup', { invite_id: await idOf(code) });
    expect(lookup).toMatchObject({ status: 404, body: { error: 'invite_unknown' } });
  };
  const packageOf = async (code: string) =>
    (
      await orchestrator.hubQuery<{ package: boolean; used: boolean; revoked: boolean }>(
        `SELECT package_ct IS NOT NULL AS package, used_at IS NOT NULL AS used, revoked_at IS NOT NULL AS revoked
           FROM hub.invite WHERE id = '${await idOf(code)}'`,
      )
    )[0];

  // Default expiries (R-1.11, 03-api.md section 12): user invitations 72 h, device invitations 24 h; at most 30 days.
  const lifetime = async (code: string) =>
    (
      await orchestrator.hubQuery<{ hours: number }>(
        `SELECT round(extract(epoch FROM expires_at - created_at) / 3600)::int AS hours FROM hub.invite
          WHERE id = '${await idOf(code)}'`,
      )
    )[0]?.hours;
  await expect(inviteByApi(ada.user, org, 31 * 24 * 3600_000)).rejects.toThrow(/400 .*request_invalid/);

  // Used: a new member joins with the code; the code is unknown afterwards, its package is gone.
  const used = await inviteMember(page, org);
  expect(await lifetime(used)).toBe(72);
  expect(await packageOf(used)).toEqual({ package: true, used: false, revoked: false });
  const erin = await joinAsNewUser(browser, e2e, used, 'Erin Member');
  expect(erin.org).toBe(org);
  await erin.context.close();
  expect(await packageOf(used)).toEqual({ package: false, used: true, revoked: false });
  await unknown(used);

  // Expired: an invitation valid for 5 s; the hub refuses it once the time has passed, the expiry job deletes it.
  const expiring = await inviteByApi(ada.user, org, 5_000);
  await anonymousBudget(COST.lookup);
  const lookup = await anonymous(stranger.page, 'POST', '/v1/invites/lookup', { invite_id: expiring.inviteId });
  expect(lookup.status).toBe(200);
  await stranger.page.waitForTimeout(6_000);
  await unknown(expiring.code);
  await expect
    .poll(async () => (await packageOf(expiring.code))?.package, { timeout: 120_000, intervals: [2_000] })
    .toBe(false);

  // Revoked: an open user invitation (members screen) and an open device invitation (device screen).
  const revokedUser = await inviteMember(page, org);
  await expect(page.getByTestId('members.invites.item')).toHaveCount(1);
  await page.getByTestId('members.invites.revoke').click();
  await expect(page.getByTestId('members.invites.empty')).toBeVisible({ timeout: 30_000 }); // a signed revoke under load
  expect(await packageOf(revokedUser)).toEqual({ package: false, used: false, revoked: true });
  await unknown(revokedUser);
  const revokedDevice = await addDeviceInvitation(page, org);
  expect(await lifetime(revokedDevice)).toBe(24);
  await page.getByTestId('devices.add.done').click();
  await expect(page.getByTestId('devices.invites.item')).toHaveCount(1);
  await page.getByTestId('devices.invites.revoke').click();
  await expect(page.getByTestId('devices.invites.empty')).toBeVisible();
  expect(await packageOf(revokedDevice)).toEqual({ package: false, used: false, revoked: true });
  await unknown(revokedDevice);
  // `agent-runner enroll` with the revoked device code is refused with the same code (no oracle, ADR-005).
  const spare = await spawnRunner(orchestrator, { device: 'org', pool_size: 0 });
  const refused = await enrollAttempt(orchestrator, spare.name, revokedDevice);
  expect(refused.rc).not.toBe(0);
  expect(`${refused.stdout}${refused.stderr}`).toContain('invite_unknown');
  await removeRunner(orchestrator, spare.name);

  // The log records invite and invite_revoke entries; the audit chain the life of each invitation.
  const log = await ada.user.log(org);
  const types = log.entries.map((entry) => entry.body.type);
  expect(types.filter((type) => type === 'invite')).toHaveLength(4);
  expect(types.filter((type) => type === 'invite_revoke')).toHaveLength(2);
  const ids = await Promise.all([used, expiring.code, revokedUser, revokedDevice].map(idOf));
  const audit = await orchestrator.hubQuery<{ target_id: string; action: string; result: string }>(
    `SELECT target_id, action, result FROM hub.audit WHERE target_kind = 'invite' AND action <> 'invite.lookup'
       AND target_id IN (${ids.map((id) => `'${id}'`).join(', ')}) ORDER BY seq`,
  );
  const actions = (id: string) => audit.filter((row) => row.target_id === id).map((row) => row.action);
  expect(actions(ids[0]!)).toEqual(expect.arrayContaining(['invite.create', 'invite.register', 'log.accept']));
  expect(actions(ids[1]!)).toEqual(expect.arrayContaining(['invite.create', 'invite.expire']));
  expect(actions(ids[2]!)).toEqual(expect.arrayContaining(['invite.create', 'invite.revoke']));
  expect(actions(ids[3]!)).toEqual(expect.arrayContaining(['invite.create', 'invite.revoke']));
  expect(audit.every((row) => row.result === 'ok')).toBe(true);
  await navigate(page, `/o/${uuid(org)}/members`);
  await expect(page.getByTestId('members.invites.empty')).toBeVisible();
  await stranger.context.close();
  await closingChecks(orchestrator);
});
