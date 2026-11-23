// E2E-02 (07-testing.md section 2.1; 04-crypto.md sections 5.1, 5.2, 5.7; 06-web.md sections 3.1, 7.1, 7.2): the
// admin of an org creates a further workgroup on the workgroup screen and invites a member on the members screen
// (role member, the further workgroup with read); the invitee opens the link in a new browser, registers a passkey,
// confirms the recovery code and joins. Asserted: the invited org is the invitee's default context (also after a
// reload); org role member (read on org level: no admin controls), write in the default workgroup, read in the extra
// one; the log's `accept` carries the invitee's signature and `inv_sig` under the code's `pk_inv`; the hub stores
// neither the code nor the names. Runs on T1 and T2.
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  openInvite,
  signIn,
  inviteMember,
  memberRow,
  navigate,
  newBrowser,
  personOf,
} from '../fixtures/identity.ts';
import { hubTablesContaining, ORG_PATH } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { b64uDecode } from '../../../web/src/core/encoding/b64u';
import { inviteSecrets, parseCode } from '../../../web/src/core/crypto/invite';
import { ed25519VerifyB64u } from '../../../web/src/core/crypto/keys';
import { logText } from '../../../web/src/core/log/entries';

const WG_NAME = 'Research kz02-wg-b8n3m1';
const INVITEE = 'Erin kz02-name-q5w7e2';

test('@E2E-02 @R-1.1 @R-1.4 @R-1.5 @R-2.2 @R-8.1 @R-8.3 @R-8.4 @R-P.5 an invited user registers, joins with the invited role and workgroups and lands in the org', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;

  // A further workgroup on the workgroup screen (the admin has write in it, 04-crypto.md section 4.7).
  await navigate(page, `/o/${org}/workgroups`);
  await page.getByTestId('workgroups.create.open').click();
  await page.getByTestId('workgroups.create.name').fill(WG_NAME);
  await page.getByTestId('workgroups.create.confirm').click();
  const item = page.getByTestId('workgroups.item').filter({ hasText: WG_NAME });
  await expect(item.getByTestId('workgroups.item.access')).toHaveText('Write');
  const extra = (await item.getAttribute('data-wg'))!;

  // The invitation on the members screen: the default role (member), the extra workgroup with read; code shown once.
  const code = await inviteMember(page, org, { wgs: { [extra]: 'read' } });
  await expect(page.getByTestId('members.invites.item')).toHaveCount(1);

  // The invitee: a new browser profile opens the link; the screen shows org, role and workgroups from the package.
  const invitee = await newBrowser(browser, e2e);
  await openInvite(invitee.page, code);
  await expect(invitee.page).toHaveURL(/\/invite$/);
  await expect(invitee.page.getByTestId('invite.role')).toHaveText('Your role: Member');
  await expect(invitee.page.getByTestId('invite.workgroup')).toHaveText([/– read and write$/, `${WG_NAME} – read`]);
  await anonymousBudget(COST.register);
  await invitee.page.getByTestId('invite.display-name').fill(INVITEE);
  await invitee.page.getByTestId('invite.create-passkey').click();
  await invitee.page.getByTestId('invite.unlock').click();
  const recoveryCode = (await invitee.page.getByTestId('recovery-code.code').textContent())!;
  await invitee.page.getByTestId('recovery-code.confirm').fill(recoveryCode.slice(-6).toLowerCase());
  await invitee.page.getByTestId('recovery-code.continue').click();
  // The invited org is the default context (R-2.2): the join lands there, and so does the next sign-in.
  await expect(invitee.page).toHaveURL(new RegExp(`/o/${org}$`), { timeout: 30_000 });
  await anonymousBudget(COST.page);
  await invitee.page.goto('/');
  await signIn(invitee.page);
  await expect(invitee.page).toHaveURL(new RegExp(`/o/${org}$`), { timeout: 30_000 });
  expect(ORG_PATH.exec(invitee.page.url())![1]).toBe(org);
  const erin = await PageUser.open(invitee.page, recoveryCode);
  const me = (await erin.request('GET', '/v1/me')) as { orgs: { org_id: string; role: string }[] };
  expect(me.orgs).toEqual([expect.objectContaining({ org_id: org, role: 'member' })]);

  // The log: accept with both signatures; role member, write in the default workgroup, read in the extra one.
  const log = await ada.user.log(org);
  const accept = log.entries.at(-1)!;
  expect(accept.body).toMatchObject({
    type: 'accept',
    kind: 'user',
    subject: erin.userId,
    pub: erin.identity.public(),
  });
  const secrets = await inviteSecrets(parseCode(code));
  await ed25519VerifyB64u(b64uDecode(secrets.pkInv), accept.inv_sig, logText(accept.body));
  await ed25519VerifyB64u(b64uDecode(erin.identity.public().sig), accept.sig, logText(accept.body));
  expect(log.state.member(erin.userId)).toMatchObject({ role: 'member' });
  expect(log.state.wgAccess(erin.userId, log.state.defaultWg)).toBe('write');
  expect(log.state.wgAccess(erin.userId, extra)).toBe('read');
  const [row] = await orchestrator.hubQuery<{ used_by: string; package: boolean }>(
    `SELECT used_by::text, package_ct IS NOT NULL AS package FROM hub.invite WHERE id = '${secrets.inviteId.replace(/[^A-Za-z0-9_-]/g, '')}'`,
  );
  expect(row).toEqual({ used_by: erin.userId, package: false });

  // Member = read on org level: the admin sees the new member live, the member sees no admin controls.
  await expect(memberRow(page, INVITEE).getByTestId('members.item.role')).toHaveText('Member');
  await navigate(invitee.page, `/o/${org}/members`);
  await expect(memberRow(invitee.page, INVITEE).getByTestId('members.item.role')).toHaveText('Member');
  await expect(invitee.page.getByTestId('members.invite.open')).toHaveCount(0);
  await expect(invitee.page.getByTestId('members.item.remove')).toHaveCount(0);
  await navigate(invitee.page, `/o/${org}/workgroups`);
  await expect(invitee.page.getByTestId('workgroups.item')).toHaveCount(2);
  await expect(
    invitee.page.getByTestId('workgroups.item').filter({ hasText: WG_NAME }).getByTestId('workgroups.item.access'),
  ).toHaveText('Read');
  await expect(invitee.page.getByTestId('workgroups.create.open')).toHaveCount(0);

  // Zero knowledge: neither the code nor the names reach the hub in plaintext.
  expect(
    await hubTablesContaining(orchestrator, [code, code.replace(/-/g, ''), 'kz02-wg-b8n3m1', 'kz02-name-q5w7e2']),
  ).toEqual([]);
  await invitee.context.close();
  await closingChecks(orchestrator);
});
