// E2E-45 (07-testing.md section 2.1; ADR-058; 04-crypto.md sections 4.7, 5.11, 6.3; 06-web.md sections 5.1, 7.1):
// organization admins read every workgroup. An org of four people (two admins, two members): an
// admin reads a workgroup it is no member of
// - existing, with its history (two epochs): while no key holder has been online the view shows the calm "being
//   unlocked" line (no error, no key wording); once a member of the workgroup signs in, its browser delivers every
//   epoch by catch-up and the content appears without the admin doing anything;
// - newly created: the creating batch already carries the wraps for every admin;
// - after an appointment: the appointing browser wraps every epoch it holds in the same batch.
// The inherited right does not write. A demotion is one batch (`role` + one `rotate` per workgroup the person read
// only as admin, wraps for the remaining readers): the hub refuses the demoted person at once, no wrap of a new epoch
// exists for it, content written afterwards stays closed to its keys; its default workgroup stays. The hub plaintext
// scan stays green.
import { expect, test } from '../fixtures/test.ts';
import {
  closingChecks,
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
import { epochKey, WG } from '../../../web/src/core/log/state';
import type { Page } from '@playwright/test';

const OLD_WG = 'Finance kz45-old-c3d5f7';
const NEW_WG = 'Sales kz45-new-g2h4j6';
const FIRST = 'First epoch kz45-first-k1m3n5';
const SECOND = 'Second epoch kz45-second-p4q6r8';
const FRESH = 'New workgroup kz45-fresh-s5t7v9';
const AFTER = 'After the demotion kz45-after-w6x8y2';
/** Wording of the key handling that must not reach a user (ADR-058 section 4). */
const KEY_WORDING = /\bkeys?\b|rotat|wrap|epoch|catch-up|Schlüssel/i;

async function createWorkgroup(page: Page, org: string, name: string): Promise<string> {
  await navigate(page, `/o/${org}/workgroups`);
  await page.getByTestId('workgroups.create.open').click();
  await page.getByTestId('workgroups.create.name').fill(name);
  await page.getByTestId('workgroups.create.confirm').click();
  const item = page.getByTestId('workgroups.item').filter({ hasText: name });
  await expect(item).toBeVisible({ timeout: 30_000 });
  return (await item.getAttribute('data-wg'))!;
}

async function writeEntry(page: Page, org: string, wg: string, text: string): Promise<void> {
  await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(text);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: text })).toBeVisible({ timeout: 30_000 });
}

async function expectEntries(page: Page, texts: readonly string[]): Promise<void> {
  for (const text of texts)
    await expect(page.getByTestId('whiteboard.item').filter({ hasText: text })).toBeVisible({ timeout: 90_000 });
}

async function changeRole(page: Page, org: string, name: string): Promise<void> {
  await navigate(page, `/o/${org}/members`);
  await memberRow(page, name).getByTestId('members.item.role-change').click();
  await page.getByTestId('members.role.confirm').click();
  await expect(page.getByTestId('members.role')).toHaveCount(0, { timeout: 30_000 });
}

test('@E2E-45 @R-8.1 @R-8.5 admins read every workgroup without anybody acting; a demotion closes new content', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;

  // An existing workgroup with a member and two epochs of content, before the second admin joins.
  const old = await createWorkgroup(page, org, OLD_WG);
  const carol = await joinAsNewUser(
    browser,
    e2e,
    await inviteMember(page, org, { wgs: { [old]: 'write' } }),
    'Carol Member',
  );
  const dan = await joinAsNewUser(browser, e2e, await inviteMember(page, org), 'Dan Member');
  await writeEntry(carol.page, org, old, FIRST);
  // a read grant and its withdrawal for Dan: the ungrant rotates the workgroup (second epoch)
  await navigate(page, `/o/${org}/members`);
  await memberRow(page, 'Dan Member').getByTestId('members.item.grant').click();
  await page.getByTestId('members.grant.wg').selectOption(old);
  await page.getByTestId('members.grant.access').selectOption('read');
  await page.getByTestId('members.grant.confirm').click();
  await expect(memberRow(page, 'Dan Member').getByTestId('members.item.access')).toContainText('Finance');
  await memberRow(page, 'Dan Member').getByTestId('members.item.ungrant').click();
  await page.getByTestId('members.ungrant.confirm').click();
  await expect(page.getByTestId('members.ungrant')).toHaveCount(0, { timeout: 30_000 });
  await expect.poll(async () => (await ada.user.log(org)).state.epoch(WG, old), { timeout: 30_000 }).toBe(2);
  await writeEntry(carol.page, org, old, SECOND);

  // Every holder of the workgroup's keys goes offline; the second admin joins.
  const adminCode = await inviteMember(page, org, { role: 'admin' });
  await page.goto('about:blank');
  await carol.page.goto('about:blank');
  const bob = await joinAsNewUser(browser, e2e, adminCode, 'Bob Admin');
  let log = await memberLog(bob.user, org);
  expect(log.state.wgAccess(bob.user.userId, old)).toBe('read');
  expect(log.state.inherited(bob.user.userId, old)).toBe(true);

  // Being unlocked: the workgroup is listed, its content waits for a key holder – calm, no key wording.
  await navigate(bob.page, `/o/${org}/workgroups`);
  await expect(
    bob.page.getByTestId('workgroups.item').filter({ hasText: OLD_WG }).getByTestId('workgroups.item.access'),
  ).toHaveText('Read');
  await navigate(bob.page, `/o/${org}/w/${old}/whiteboard`);
  const unlocking = bob.page.getByTestId('unlocking');
  await expect(unlocking).toHaveAttribute('data-status', 'unlocking', { timeout: 60_000 });
  await expect(unlocking).toHaveText(/Being unlocked/);
  await expect(bob.page.locator('main')).not.toContainText(KEY_WORDING);
  await expect(bob.page.getByRole('alert')).toHaveCount(0);

  // The member signs in again: its browser delivers both epochs; the admin's view fills by itself.
  await visit(carol.page, `/o/${org}`);
  await expectEntries(bob.page, [FIRST, SECOND]);
  await expect(unlocking).toHaveCount(0);
  await expect(bob.page.getByTestId('whiteboard.create')).toHaveCount(0);
  const bobKeys = await bob.user.keys(org, await memberLog(bob.user, org));
  expect(bobKeys.has(epochKey(WG, old, 1)) && bobKeys.has(epochKey(WG, old, 2))).toBe(true);

  // A new workgroup: readable for the other admin at once.
  await visit(page, `/o/${org}`);
  const fresh = await createWorkgroup(page, org, NEW_WG);
  await writeEntry(page, org, fresh, FRESH);
  await navigate(bob.page, `/o/${org}/w/${fresh}/whiteboard`);
  await expectEntries(bob.page, [FRESH]);
  const createWraps = await orchestrator.hubQuery<{ recipient_id: string }>(
    `SELECT recipient_id::text FROM hub.key_wrap WHERE scope = 'wg' AND scope_id = '${uuid(fresh)}' AND epoch = 1`,
  );
  expect(createWraps.map((row) => row.recipient_id)).toContain(bob.user.userId);

  // An appointment: the new admin reads both workgroups, history included; it still writes neither.
  await changeRole(page, org, 'Dan Member');
  await expect(memberRow(page, 'Dan Member').getByTestId('members.item.access')).toContainText('reads as admin');
  await navigate(dan.page, `/o/${org}/w/${old}/whiteboard`);
  await expectEntries(dan.page, [FIRST, SECOND]);
  await navigate(dan.page, `/o/${org}/w/${fresh}/whiteboard`);
  await expectEntries(dan.page, [FRESH]);
  await expect(dan.page.getByTestId('whiteboard.create')).toHaveCount(0);

  // The demotion: one batch with the role change and the rotation of both workgroups.
  const before = await ada.user.log(org);
  await navigate(page, `/o/${org}/members`);
  await memberRow(page, 'Bob Admin').getByTestId('members.item.role-change').click();
  await expect(page.getByTestId('members.role.demote')).toBeVisible();
  await expect(page.getByTestId('members.role')).not.toContainText(KEY_WORDING);
  await page.getByTestId('members.role.confirm').click();
  await expect(page.getByTestId('members.role')).toHaveCount(0, { timeout: 30_000 });
  log = await ada.user.log(org);
  const batch = log.entries.slice(before.entries.length);
  expect(batch.map((entry) => entry.body.type)).toEqual(['role', 'rotate', 'rotate']);
  expect([log.state.epoch(WG, old), log.state.epoch(WG, fresh)]).toEqual([3, 2]);
  expect(log.state.wgAccess(bob.user.userId, old)).toBeNull();
  expect(log.state.wgAccess(bob.user.userId, log.state.defaultWg)).toBe('write');
  expect(await refusal(bob.user, 'GET', `/v1/scopes/${old}/events`)).toBe('404 not_found');
  expect(await refusal(bob.user, 'GET', `/v1/scopes/${fresh}/events`)).toBe('404 not_found');
  const newWraps = await orchestrator.hubQuery<{ recipient_id: string }>(
    `SELECT recipient_id::text FROM hub.key_wrap WHERE scope = 'wg'
      AND ((scope_id = '${uuid(old)}' AND epoch = 3) OR (scope_id = '${uuid(fresh)}' AND epoch = 2))`,
  );
  expect(newWraps.map((row) => row.recipient_id)).not.toContain(bob.user.userId);
  for (const reader of [carol.user.userId, dan.user.userId])
    expect(newWraps.map((row) => row.recipient_id)).toContain(reader);

  // New content stays closed to the demoted admin's keys; the remaining readers see it.
  await writeEntry(page, org, old, AFTER);
  const view = await openScopeEvents(orchestrator, ada.user, { kind: WG, id: old }, bobKeys, ['whiteboard.entry']);
  expect(view.opened.join('\n')).toContain('kz45-second-p4q6r8');
  expect(view.opened.join('\n')).not.toContain('kz45-after-w6x8y2');
  expect(view.locked).toEqual([3]);
  await navigate(dan.page, `/o/${org}/w/${old}/whiteboard`);
  await expectEntries(dan.page, [AFTER]);

  await expectNoPlaintext(orchestrator, ['kz45-old-c3d5f7', 'kz45-new-g2h4j6', FIRST, SECOND, FRESH, AFTER]);
  for (const other of [bob, carol, dan]) await other.context.close();
  await closingChecks(orchestrator);
});
