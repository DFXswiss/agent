// E2E-41 (ADR-060; 04-crypto.md sections 4.8, 5.13; 06-web.md section 7.1): an org admin admits an existing account
// directly. The operator certifies the identities in the platform log (`certify-user`, Bob's with the account code
// his own browser shows); the platform device works in a team workgroup of Ada's org and announced `log.admit`. Ada
// types Bob's account code on the members screen and admits him (member, write on the workgroup): one `admit` entry,
// no invitation, no `accept`. Bob does nothing: after the next page load the org is in his switcher with an
// `org.admitted` notification; he reads what Ada wrote in the workgroup and his prompt runs on the platform device
// (the runner verified a log with `admit`). A non-admin cannot admit (no button; the hub refuses a signed entry). Bob
// leaves; Ada's browser sends the owed rotation. Ada's agent tool (`agent-platform org admit`) admits Carol by user ID.
// Names (ADR-067, HOT-12): Ada gives Bob a name when admitting him and sees it at once (marked as provisional), Bob's
// client writes his own name when he opens the org without any action of his, and it replaces the provisional one;
// the agent admits Carol with a name and changes it with `org member-name`; no member line shows an ID fragment and
// the hub never sees a name. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { closingChecks, memberLog, navigate, person, personOf, refusal, visit } from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { agentTool, pairAgent } from '../fixtures/agent-tool.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { formatTime } from '../../../web/src/core/encoding/time';
import { buildEntries } from '../../../web/src/core/log/entries';
import type { Page } from '@playwright/test';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-41-direct-admission.json', import.meta.url), 'utf8'),
);
const WG_NAME = 'Team kz40-wg-q2w4e6';
const BOARD = 'Ada wrote kz40-board-r5t7y9';
const ADMIN_PROMPT = 'E2E40-ADMIN kz40-prompt-a-u1i3o5: hello';
const ADMITTED_PROMPT = 'E2E40-ADMITTED kz40-prompt-b-p6l8k0: hello';
const CANARIES = [
  'kz40-wg-q2w4e6',
  'kz40-board-r5t7y9',
  'kz40-prompt-a-u1i3o5',
  'kz40-prompt-b-p6l8k0',
  'kz40-answer-a3c5e7',
  'kz40-answer-b4d6f8',
  'kz40-name-bob-h7j9k1',
  'kz40-name-carol-m2n4b6',
  'kz40-name-carol2-v8c0x2',
  'kz40-name-dora-s3d5f7',
];
const DORA = 'Dora kz40-name-dora-s3d5f7';
const BOB_GIVEN = 'Bob kz40-name-bob-h7j9k1';
const CAROL_GIVEN = 'Carol kz40-name-carol-m2n4b6';
const CAROL_CHANGED = 'Carol kz40-name-carol2-v8c0x2';

/** The member list never shows an ID or a fragment of one as a name (ADR-067). */
async function namesWithoutIds(page: Page, ids: readonly string[]): Promise<void> {
  for (const text of await page.getByTestId('members.item.name').allTextContents()) {
    expect(text).not.toMatch(/…[0-9a-f]{4,}/);
    expect(text).not.toMatch(/^[0-9a-f-]{6,}$/);
    for (const id of ids) {
      expect(text).not.toContain(id.slice(-6));
      expect(text).not.toContain(id.slice(0, 8));
    }
  }
}

async function accountCode(page: Page): Promise<string> {
  const back = new URL(page.url()).pathname;
  await navigate(page, '/settings');
  const code = (await page.getByTestId('passkeys.account-code.value').textContent())!.trim();
  expect(code).toMatch(/^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$/);
  await navigate(page, back);
  return code;
}

const orgsOf = async (user: { request: (method: string, target: string) => Promise<unknown> }) =>
  ((await user.request('GET', '/v1/me')) as { orgs: { org_id: string }[] }).orgs.map((item) => item.org_id).sort();

test('@E2E-41 @R-1.13 @needs-vm an admin admits an existing account directly and the admitted member works without any step', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator, 1);

  // 1. Ada (admin of her org), Bob and Carol: existing accounts, each in its own browser.
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;
  const bob = await person(browser, e2e, orchestrator, 'Bob Admitted');
  const carol = await person(browser, e2e, orchestrator, 'Carol Agent');
  const bobCode = await accountCode(bob.page);

  // 2. The operator certifies the identities (Bob's against the code his browser computed).
  const certified = await operator(orchestrator, [
    'certify-user',
    '--user',
    bob.user.userId,
    '--code',
    bobCode,
    '--user',
    ada.user.userId,
    '--user',
    carol.user.userId,
  ]);
  expect(certified).toContain(`user ${bob.user.userId} account code ${bobCode}`);
  for (const user of [bob, ada, carol]) expect(certified).toContain(`certified user ${user.user.userId}`);

  // 3. A team workgroup in which the platform device works (Ada's first task assigns it); the device announced
  //    `log.admit`. (A reload: the page reads the platform log with the certifications.)
  await visit(page, `/o/${org}/workgroups`);
  await page.getByTestId('workgroups.create.open').click();
  await page.getByTestId('workgroups.create.name').fill(WG_NAME);
  await page.getByTestId('workgroups.create.confirm').click();
  const wgItem = page.getByTestId('workgroups.item').filter({ hasText: WG_NAME });
  await expect(wgItem).toHaveCount(1);
  const wg = (await wgItem.getAttribute('data-wg'))!;
  await home(page);
  await expect(page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(1, {
    timeout: 30_000,
  });
  await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
  const adminTask = await sendPrompt(page, ADMIN_PROMPT);
  await expectAnswerInChat(page, 'Admin task kz40-answer-a3c5e7.');
  expect((await ada.user.log(org)).state.assigned(device, wg)).toBe(true);
  const [features] = await orchestrator.hubQuery<{ features: string[] }>(
    `SELECT features FROM hub.device WHERE id = '${uuid(device)}'`,
  );
  expect(features!.features).toContain('log.admit');
  await navigate(page, `/o/${org}/w/${wg}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(BOARD);
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: BOARD })).toBeVisible({ timeout: 30_000 });

  // 4. Ada admits Bob with his account code: member, write on the team workgroup.
  const before = await ada.user.log(org);
  await navigate(page, `/o/${org}/members`);
  await page.getByTestId('members.admit.open').click();
  await page.getByTestId('members.admit.code').fill(bobCode);
  await page.getByTestId('members.admit.find').click();
  await expect(page.getByTestId('members.admit.found')).toContainText(bobCode, { timeout: 30_000 });
  await expect(page.getByTestId('members.admit.role')).toHaveValue('member');
  await page.getByTestId(`members.admit.wg.${wg}`).selectOption('write');
  await page.getByTestId('members.admit.name').fill(BOB_GIVEN);
  await page.getByTestId('members.admit.submit').click();
  await expect(page.getByTestId('members.admit.done')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('members.admit.error')).toHaveCount(0);
  await expect(page.getByTestId('members.admit.done')).toContainText(BOB_GIVEN);
  // every member sees the name at once, marked as given by the admin
  const bobEntry = page.locator(`[data-testid="members.item"][data-member="${bob.user.userId}"]`);
  await expect(bobEntry.getByTestId('members.item.name')).toHaveText(BOB_GIVEN, { timeout: 30_000 });
  await expect(bobEntry.getByTestId('members.item.name')).toHaveAttribute('data-source', 'admin');
  await expect(bobEntry.getByTestId('members.item.given')).toBeVisible();
  await namesWithoutIds(page, [ada.user.userId, bob.user.userId]);

  const after = await ada.user.log(org);
  const added = after.entries.slice(before.entries.length);
  expect(added.map((entry) => entry.body.type)).toEqual(['admit']);
  expect(added[0]!.body).toMatchObject({
    type: 'admit',
    by: ada.user.userId,
    subject: bob.user.userId,
    pub: bob.user.identity.public(),
    role: 'member',
    wgs: { [wg]: 'write' },
  });
  expect(after.entries.some((entry) => entry.body.type === 'accept')).toBe(false);
  expect(after.state.member(bob.user.userId)).toMatchObject({ role: 'member' });
  expect(after.state.wgAccess(bob.user.userId, wg)).toBe('write');
  expect(await orchestrator.hubQuery(`SELECT id FROM hub.invite WHERE org_id = '${uuid(org)}'`)).toEqual([]);
  expect(
    await orchestrator.hubQuery<{ consented: boolean }>(
      `SELECT consented FROM hub.org_member WHERE org_id = '${uuid(org)}' AND principal_id = '${uuid(bob.user.userId)}'`,
    ),
  ).toEqual([{ consented: false }]);
  expect(
    await orchestrator.hubQuery<{ kind: string; ref: string }>(
      `SELECT kind, ref FROM hub.notification WHERE recipient_id = '${uuid(bob.user.userId)}'
          AND org_id = '${uuid(org)}'`,
    ),
  ).toEqual([{ kind: 'org.admitted', ref: ada.user.userId }]);

  // 5. Bob without any confirmation: after the next page load the notification and the org are there; he reads Ada's
  //    entry and his prompt runs on the platform device.
  await visit(bob.page, '/notifications');
  const notice = bob.page.locator('[data-testid="notifications.item"][data-kind="org.admitted"]');
  await expect(notice).toHaveCount(1, { timeout: 30_000 });
  await expect(notice.getByTestId('notifications.text')).toContainText('admitted you');
  expect(await orgsOf(bob.user)).toEqual([org, bob.onboarded.org].sort());
  await bob.page.getByTestId('org-switcher.current').click();
  await expect(bob.page.getByTestId('org-switcher.item')).toHaveCount(2);
  await expect(bob.page.locator(`[data-testid="org-switcher.item"][data-org="${uuid(org)}"]`)).toBeVisible();
  await bob.page.keyboard.press('Escape');
  await navigate(bob.page, `/o/${org}/w/${wg}/whiteboard`);
  await expect(bob.page.getByTestId('whiteboard.item').filter({ hasText: BOARD })).toBeVisible({ timeout: 30_000 });
  // opening the org wrote Bob's own name there, without any action of his; it replaces the provisional one for Ada
  await expect
    .poll(
      async () => {
        await visit(page, `/o/${org}/members`);
        return bobEntry.getByTestId('members.item.name').getAttribute('data-source', { timeout: 30_000 });
      },
      { timeout: 90_000, intervals: [3000] },
    )
    .toBe('own');
  await expect(bobEntry.getByTestId('members.item.name')).toHaveText('Bob Admitted');
  await expect(bobEntry.getByTestId('members.item.given')).toHaveCount(0);
  await namesWithoutIds(page, [ada.user.userId, bob.user.userId]);
  await navigate(bob.page, `/o/${org}`);
  const select = bob.page.getByTestId('home.wg');
  await expect(select.locator('option', { hasText: WG_NAME })).toHaveCount(1, { timeout: 30_000 });
  await select.selectOption({ label: WG_NAME });
  const admittedTask = await sendPrompt(bob.page, ADMITTED_PROMPT);
  await expectAnswerInChat(bob.page, 'Admitted member task kz40-answer-b4d6f8.');
  const tasks = await orchestrator.hubQuery<{ id: string; wg_id: string }>(
    `SELECT id, wg_id::text FROM hub.task WHERE org_id = '${uuid(org)}' ORDER BY created_at`,
  );
  expect(tasks.map((task) => [task.id, task.wg_id])).toEqual([
    [adminTask, wg],
    [admittedTask, wg],
  ]);
  const [session] = await mainSessions(orchestrator, admittedTask);
  expect(session).toMatchObject({ device_id: device });

  // 6. A non-admin cannot admit: no button for Bob; the hub refuses a signed `admit` of his.
  await navigate(bob.page, `/o/${org}/members`);
  await expect(bob.page.getByTestId('members.screen')).toBeVisible();
  await expect(bob.page.getByTestId('members.leave')).toBeVisible();
  await expect(bob.page.getByTestId('members.admit.open')).toHaveCount(0);
  const bobView = await memberLog(bob.user, org);
  const carolIdentity = (await bob.user.request('GET', `/v1/users/${carol.user.userId}/identity`)) as {
    pub: { kem: string; sig: string };
    platform_seq: number;
    id_sig: string;
  };
  const bobIdentity = (await bob.user.request('GET', `/v1/users/${bob.user.userId}/identity`)) as {
    platform_seq: number;
    id_sig: string;
  };
  const [forged] = await buildEntries(
    { org, head: bobView.head },
    { id: bob.user.userId, key: bob.user.identity.sig.privateKey },
    [
      {
        type: 'admit',
        subject: carol.user.userId,
        pub: carolIdentity.pub,
        platform_seq: carolIdentity.platform_seq,
        id_sig: carolIdentity.id_sig,
        by_platform_seq: bobIdentity.platform_seq,
        by_id_sig: bobIdentity.id_sig,
        role: 'member',
        wgs: {},
      },
    ],
    formatTime(Date.now()),
  );
  const refused = await refusal(bob.user, 'POST', `/v1/orgs/${org}/log`, {
    expected_seq: bobView.head.seq + 1,
    entries: [forged],
    wraps: [],
    events: [],
  });
  expect(refused).toMatch(/^4\d\d (log_rights|forbidden)$/);
  expect((await ada.user.log(org)).state.member(carol.user.userId)).toBeNull();

  // 8. The agent path: Ada's paired agent admits Carol by user ID (`agent-platform org admit`); Carol sees the org.
  await pairAgent(orchestrator, page, e2e.hubOrigin, 'ada');
  const admitted = await agentTool(orchestrator, 'ada', [
    'org',
    'admit',
    '--org',
    org,
    '--user',
    carol.user.userId,
    '--name',
    CAROL_GIVEN,
  ]);
  expect(admitted).toMatchObject({ org_id: org, user_id: carol.user.userId, role: 'member', name_saved: true });
  // the agent changes the provisional name; the browser shows the newest one
  const renamed = await agentTool(orchestrator, 'ada', [
    'org',
    'member-name',
    '--org',
    org,
    '--user',
    carol.user.userId,
    '--name',
    CAROL_CHANGED,
  ]);
  expect(renamed).toMatchObject({ subject: carol.user.userId, name: CAROL_CHANGED });
  const shown = (await agentTool(orchestrator, 'ada', ['org', 'show', '--org', org])) as {
    members: { principal_id: string; name: string | null; name_source: string }[];
  };
  expect(shown.members.find((m) => m.principal_id === carol.user.userId)).toMatchObject({
    name: CAROL_CHANGED,
    name_source: 'admin',
  });
  await visit(page, `/o/${org}/members`);
  const carolEntry = page.locator(`[data-testid="members.item"][data-member="${carol.user.userId}"]`);
  await expect(carolEntry.getByTestId('members.item.name')).toHaveText(CAROL_CHANGED, { timeout: 30_000 });
  const viaAgent = (await ada.user.log(org)).entries.at(-1)!.body;
  expect(viaAgent).toMatchObject({ type: 'admit', subject: carol.user.userId, pub: carol.user.identity.public() });
  expect(await orgsOf(carol.user)).toEqual([org, carol.onboarded.org].sort());
  await visit(carol.page, '/notifications');
  await expect(carol.page.locator('[data-testid="notifications.item"][data-kind="org.admitted"]')).toHaveCount(1, {
    timeout: 30_000,
  });
  await carol.page.getByTestId('org-switcher.current').click();
  await expect(carol.page.locator(`[data-testid="org-switcher.item"][data-org="${uuid(org)}"]`)).toBeVisible();
  await carol.context.close();

  await visit(page, `/o/${org}/members`);
  // an invitation says for whom it is (the name is sealed like the member names)
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.name').fill(DORA);
  await page.getByTestId('members.invite.submit').click();
  await page.getByTestId('members.invite.done').click({ timeout: 30_000 });
  const doraInvite = page.getByTestId('members.invites.item').filter({ hasText: DORA });
  await expect(doraInvite.getByTestId('members.invites.for')).toHaveText(`Invitation for ${DORA}`, {
    timeout: 30_000,
  });
  await doraInvite.getByTestId('members.invites.revoke').click();
  await expect(page.getByTestId('members.invites.item').filter({ hasText: DORA })).toHaveCount(0, { timeout: 30_000 });

  // 7. Bob leaves; the org is gone for him; Ada's browser sends the rotation his leaving owes.
  await visit(page, `/o/${org}/members`);
  const leaving = await ada.user.log(org);
  await bob.page.getByTestId('members.leave').click();
  await bob.page.getByTestId('members.leave-dialog.confirm').click();
  await expect(bob.page).toHaveURL(new RegExp(`/o/${bob.onboarded.org}$`), { timeout: 30_000 });
  expect(await orgsOf(bob.user)).toEqual([bob.onboarded.org]);
  await navigate(bob.page, `/o/${org}/members`);
  await expect(bob.page.getByTestId('members.not-found')).toBeVisible();
  await expect
    .poll(async () => (await ada.user.log(org)).state.pendingScopes().length, { timeout: 60_000, intervals: [1000] })
    .toBe(0);
  const settled = await ada.user.log(org);
  const tail = settled.entries.slice(leaving.entries.length).map((entry) => entry.body);
  expect(tail[0]).toMatchObject({ type: 'remove', by: bob.user.userId, subject: bob.user.userId });
  expect(tail.slice(1).map((body) => body.type)).toContain('rotate');
  expect(settled.state.member(bob.user.userId)).toBeNull();
  await bob.context.close();

  await expectNoPlaintext(orchestrator, CANARIES);
  await closingChecks(orchestrator, true);
});
