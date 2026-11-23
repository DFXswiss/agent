// E2E-31 (07-testing.md section 2.1; 04-crypto.md sections 4.7, 5.10, 6.3; 06-web.md section 7.1): an admin removes a
// member. The removal is one atomic batch (`remove` + one `rotate` per scope the member could read, wraps of the new
// epochs for the remaining recipients); content written before stays readable for the remaining members (and – as
// documented – for what the removed member already read); the hub refuses the removed member at once; content written
// after the removal is under the new epoch, which the removed member's keys cannot open and for which the hub holds no
// wrap of it. Runs on T1 and T2.
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  signIn,
  inviteMember,
  joinAsNewUser,
  memberLog,
  memberRow,
  navigate,
  openScopeEvents,
  personOf,
  refusal,
  visit,
} from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { uuid } from '../fixtures/platform.ts';
import { epochKey, ORG, WG } from '../../../web/src/core/log/state';
import type { Page } from '@playwright/test';

const OLD_TEXT = 'Before the removal kz31-old-p3r5t7';
const NEW_TEXT = 'After the removal kz31-new-u2w4y6';

async function writeEntry(page: Page, org: string, wg: string, text: string): Promise<void> {
  await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(text);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: text })).toBeVisible({ timeout: 30_000 });
}

test('@E2E-31 @R-5.4 @R-5.6 @R-14.1 @R-P.5 a removed member is refused and cannot decrypt what is written after the rotation', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;
  const bob = await joinAsNewUser(browser, e2e, await inviteMember(page, org), 'Bob Leaving');
  const carol = await joinAsNewUser(browser, e2e, await inviteMember(page, org), 'Carol Staying');
  const before = await ada.user.log(org);
  const wg = before.state.defaultWg;
  expect([before.state.epoch(ORG, org), before.state.epoch(WG, wg)]).toEqual([1, 1]);

  // Content before the removal, read by the member that will be removed.
  await writeEntry(page, org, wg, OLD_TEXT);
  await visit(bob.page, `/o/${org}/w/${wg}/whiteboard`);
  await expect(bob.page.getByTestId('whiteboard.item').filter({ hasText: OLD_TEXT })).toBeVisible();
  const bobKeys = await bob.user.keys(org, await memberLog(bob.user, org));
  expect([...bobKeys.keys()].sort()).toEqual([epochKey(ORG, org, 1), epochKey(WG, wg, 1)].sort());

  // The removal on the members screen: one batch with remove and the rotations of the org and the workgroup.
  await navigate(page, `/o/${org}/members`);
  await memberRow(page, 'Bob Leaving').getByTestId('members.item.remove').click();
  await page.getByTestId('members.remove.confirm').click();
  await expect(page.getByTestId('members.remove')).toHaveCount(0, { timeout: 30_000 });
  await expect(memberRow(page, 'Bob Leaving')).toHaveCount(0);
  const after = await ada.user.log(org);
  const batch = after.entries.slice(before.entries.length); // whiteboard entries are events, not log entries
  expect(batch.map((entry) => entry.body.type)).toEqual(['remove', 'rotate', 'rotate']);
  const first = batch[0]!.body.seq as number;
  expect(batch.map((entry) => entry.body.seq)).toEqual([first, first + 1, first + 2]);
  expect(after.state.member(bob.user.userId)).toBeNull();
  expect([after.state.epoch(ORG, org), after.state.epoch(WG, wg)]).toEqual([2, 2]);
  // the batch arrived in one request: one membership append call with all three entries (audit rows share a time)
  const appended = await orchestrator.hubQuery<{ type: string; received_at: string }>(
    `SELECT type, received_at::text FROM hub.membership_entry WHERE org_id = '${uuid(org)}' AND seq >= ${first}
      ORDER BY seq`,
  );
  expect(appended.map((row) => row.type)).toEqual(['remove', 'rotate', 'rotate']);
  expect(new Set(appended.map((row) => row.received_at)).size).toBe(1);
  // wraps of the new epochs for every remaining member and none for the removed one
  const wraps = await orchestrator.hubQuery<{ recipient_id: string; scope: string }>(
    `SELECT recipient_id::text, scope FROM hub.key_wrap WHERE epoch = 2 AND scope_id IN ('${uuid(org)}', '${uuid(wg)}')`,
  );
  for (const member of [ada.user.userId, carol.user.userId])
    expect(
      wraps
        .filter((row) => row.recipient_id === member)
        .map((row) => row.scope)
        .sort(),
    ).toEqual(['org', 'wg']);
  expect(wraps.filter((row) => row.recipient_id === bob.user.userId)).toEqual([]);

  // The hub refuses the removed member at once.
  // (reads without the right are `404 not_found`: no existence oracle, decision SCN-03 7)
  expect(await refusal(bob.user, 'GET', `/v1/orgs/${org}`)).toBe('404 not_found');
  expect(await refusal(bob.user, 'GET', `/v1/scopes/${wg}/events`)).toBe('404 not_found');
  const own = (await bob.user.request('GET', '/v1/wraps')) as { wraps: { scope_id: string; epoch: number }[] };
  expect(own.wraps.filter((wrap) => [org, wg].includes(wrap.scope_id) && wrap.epoch === 2)).toEqual([]);

  // Content after the removal: the remaining members read old and new content.
  await writeEntry(page, org, wg, NEW_TEXT);
  await visit(carol.page, `/o/${org}/w/${wg}/whiteboard`);
  for (const text of [OLD_TEXT, NEW_TEXT])
    await expect(carol.page.getByTestId('whiteboard.item').filter({ hasText: text })).toBeVisible({ timeout: 30_000 });
  const carolKeys = await carol.user.keys(org, await memberLog(carol.user, org));
  const carolView = await openScopeEvents(orchestrator, carol.user, { kind: WG, id: wg }, carolKeys, [
    'whiteboard.entry',
  ]);
  expect(carolView.locked).toEqual([]);
  expect(carolView.opened.join('\n')).toContain('kz31-new-u2w4y6');
  expect(carolView.opened.join('\n')).toContain('kz31-old-p3r5t7');

  // The removed member's keys open the old entry; every one of them fails on the entry of the new epoch.
  const bobView = await openScopeEvents(orchestrator, ada.user, { kind: WG, id: wg }, bobKeys, ['whiteboard.entry']);
  expect(bobView.opened.join('\n')).toContain('kz31-old-p3r5t7');
  expect(bobView.opened.join('\n')).not.toContain('kz31-new-u2w4y6');
  expect(bobView.locked).toEqual([2]);
  // the removed member's browser: after the next sign-in it has no org any more, the new content never shows
  await anonymousBudget(COST.page);
  await bob.page.goto('/');
  await signIn(bob.page);
  await expect(bob.page).toHaveURL(/\/settings$/, { timeout: 30_000 });
  await expect(bob.page.getByTestId('settings.no-org')).toBeVisible();
  await expect(bob.page.getByText(NEW_TEXT)).toHaveCount(0);
  await expectNoPlaintext(orchestrator, ['kz31-old-p3r5t7', 'kz31-new-u2w4y6']);
  await bob.context.close();
  await carol.context.close();
  await closingChecks(orchestrator);
});
