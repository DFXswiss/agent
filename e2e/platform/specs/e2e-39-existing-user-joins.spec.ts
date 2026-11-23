// E2E-39 (07-testing.md section 2.1; 04-crypto.md section 5.2a; 06-web.md section 3.1): a user with an account (own
// private org) signs out, opens an org invitation of another org, chooses "I already have an account", signs in with
// the passkey and joins. Asserted: no second identity (the `accept` carries the registered user ID and identity, the
// hub knows one user), the org switcher lists both orgs, the joined org is open. A second user cannot use an
// invitation that is bound to another user: a new person registers a passkey with a further invitation and stops
// before joining; the existing user then gets `invite_registered` on the screen and from the hub. KOR-36: the existing
// user with an open session opens a further invitation (stored session, locked keys) and joins with one action (one passkey gesture),
// a hub restart in the middle of the join is repeated with "Try again" in the same tab, and the inviter sees the
// invitation's state (open → link opened → accepted). Runs on T1 (T2 is not available, INF-05).
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  openInvite,
  signIn,
  inviteMember,
  newBrowser,
  person,
  personOf,
} from '../fixtures/identity.ts';
import { uuid } from '../fixtures/platform.ts';
import { joinWithCode } from '../../../web/e2e/helpers/members.ts';
import { inviteSecrets, parseCode } from '../../../web/src/core/crypto/invite';
import { b64uDecode } from '../../../web/src/core/encoding/b64u';

test('@E2E-39 @R-1.13 @R-P.5 an existing user joins a further org without a second identity', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;
  const bob = await person(browser, e2e, orchestrator, 'Bob Builder');

  // An invitation that a new person binds by registering a passkey (and leaving before the join).
  const bound = await inviteMember(page, org);
  const carol = await newBrowser(browser, e2e);
  await openInvite(carol.page, bound);
  await anonymousBudget(COST.register);
  await carol.page.getByTestId('invite.display-name').fill('Carol Second');
  await carol.page.getByTestId('invite.create-passkey').click();
  // the accept never arrives (with PRF the join follows the passkey at once, KOR-22): she leaves before it
  await carol.page.route('**/v1/orgs/*/log', (route) => route.abort());
  await carol.page.getByTestId('invite.unlock').click();
  await expect(carol.page.getByTestId('invite.accept-failed')).toBeVisible();
  await carol.context.close();
  const boundId = (await inviteSecrets(parseCode(bound))).inviteId;
  const [registered] = await orchestrator.hubQuery<{ registered: boolean; used: boolean }>(
    `SELECT registered_user IS NOT NULL AS registered, used_at IS NOT NULL AS used FROM hub.invite
      WHERE id = '${boundId.replace(/[^A-Za-z0-9_-]/g, '')}'`,
  );
  expect(registered).toEqual({ registered: true, used: false });

  // The existing user cannot use it: the screen ends in `invite_registered`, the hub refuses the accept (409).
  // (a reload locks the session: the screen offers the existing account and asks for the passkey)
  await openInvite(bob.page, bound);
  await expect(bob.page.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'sign_in');
  await signIn(bob.page, 'invite.join');
  await expect(bob.page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_registered');
  await anonymousBudget(COST.page);
  await bob.page.goto('/');
  await signIn(bob.page);
  await expect(bob.page).toHaveURL(new RegExp(`/o/${bob.onboarded.org}$`));
  await anonymousBudget(COST.lookup);
  await expect(joinWithCode(bob.user, e2e.hubOrigin, bound, 'Bob')).rejects.toThrow(/409 .*invite_registered/);

  // KOR-36: a fresh invitation opened anew in Bob's open session (stored session, locked keys – as in a new tab): one
  // action – the passkey gesture of "Join … with my account" – makes him a member under his account's name; until then
  // the page says he has not joined.
  // The hub is unreachable for the accept and restarts: "Try again" in the same tab succeeds, the link is not needed
  // again. The inviter sees the state of the invitation before and after.
  const code = await inviteMember(page, org);
  await anonymousBudget(COST.page);
  await page.goto(`/o/${org}/members`);
  await signIn(page);
  await expect(page.locator('[data-testid="members.invites.item"][data-status="open"]')).toHaveCount(1);
  // the link opened anew in Bob's browser profile: the session is stored, the keys are locked – the state of a new
  // tab (a passkey copied into a second tab's virtual authenticator gives no PRF output, so the original page is used)
  const tab = bob.page;
  await openInvite(tab, code);
  await expect(tab.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'sign_in');
  await expect(tab.getByTestId('invite.not-joined')).toBeVisible();
  await expect(tab.getByTestId('invite.create-passkey')).toHaveCount(0);
  // the inviter sees "link opened"
  await anonymousBudget(COST.page);
  await page.reload();
  await signIn(page);
  await expect(page.locator('[data-testid="members.invites.item"][data-status="opened"]')).toHaveCount(1, {
    timeout: 30_000,
  });
  // the hub is unreachable for the join's accept and restarts: nothing is joined, the tab offers "Try again"
  await tab.route('**/v1/orgs/*/log', (route) =>
    route.request().method() === 'POST' ? route.abort() : route.continue(),
  );
  await signIn(tab, 'invite.join');
  await expect(tab.getByTestId('invite.accept-failed')).toBeVisible({ timeout: 30_000 });
  await expect(tab.getByTestId('invite.not-joined')).toBeVisible();
  await orchestrator.call('POST', '/hub/restart', {});
  await tab.unroute('**/v1/orgs/*/log');
  await tab.getByTestId('invite.accept-retry').click();
  await expect(tab).toHaveURL(new RegExp(`/o/${org}$`), { timeout: 30_000 });
  await expect(tab.getByTestId('shell.joined')).toBeVisible();
  await anonymousBudget(COST.page);
  await page.reload();
  await signIn(page);
  await expect(page.locator('[data-testid="members.invites.item"]')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.locator('[data-testid="members.invites.accepted"]', { hasText: 'Bob Builder' })).toHaveCount(1);

  // No second identity: the accept names Bob's registered user and identity; the hub has one user for him.
  const log = await ada.user.log(org);
  const accept = log.entries.at(-1)!;
  expect(accept.body).toMatchObject({
    type: 'accept',
    kind: 'user',
    subject: bob.user.userId,
    pub: bob.user.identity.public(),
  });
  const sig = Buffer.from(b64uDecode(bob.user.identity.public().sig)).toString('hex');
  const users = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.user_identity WHERE sig_pub = decode('${sig}', 'hex')`,
  );
  expect(users).toEqual([{ n: 1 }]);
  const me = (await bob.user.request('GET', '/v1/me')) as { user_id: string; orgs: { org_id: string }[] };
  expect(me.user_id).toBe(bob.user.userId);
  expect(me.orgs.map((item) => item.org_id).sort()).toEqual([org, bob.onboarded.org].sort());
  expect(log.state.member(bob.user.userId)).toMatchObject({ role: 'member' });

  // The org switcher lists both orgs (the joined one is open).
  await tab.getByTestId('org-switcher.current').click();
  await expect(tab.getByTestId('org-switcher.item')).toHaveCount(2);
  for (const id of [org, bob.onboarded.org])
    await expect(tab.locator(`[data-testid="org-switcher.item"][data-org="${uuid(id)}"]`)).toBeVisible();
  await expect(tab.locator(`[data-testid="org-switcher.item"][data-org="${uuid(org)}"]`)).toHaveAttribute(
    'aria-current',
    'true',
  );
  await tab.keyboard.press('Escape');

  // The used invitation is gone for everybody else.
  const dave = await newBrowser(browser, e2e);
  await openInvite(dave.page, code);
  await expect(dave.page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
  await dave.context.close();
  await bob.context.close();
  await closingChecks(orchestrator);
});
