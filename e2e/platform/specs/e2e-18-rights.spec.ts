// E2E-18 (07-testing.md section 2.1; 04-crypto.md sections 4.3, 4.7, 6.4; 03-api.md sections 3, 4; 06-web.md sections
// 7.1, 7.2): rights in the backend and in the user interface. An admin creates a further workgroup (the creating admin
// gets write), invites a member and a second admin and grants the member read access on the further workgroup on the
// members screen (a person added at workgroup level); the second admin reads it by inheritance (ADR-058: every admin
// reads every workgroup of the org) but cannot write it without a `grant`. The member's and the second admin's
// screens hide what they may not do; direct API calls with their sessions for
// forbidden writes get `403 forbidden` (log entries without the right `log_rights`, events `author_not_writer`; reads
// without the right `404 not_found`, the hub's rule against existence oracles). An event the hub refused
// – the member's whiteboard entry in the workgroup it only reads – stored by a tampered hub is flagged
// `author_not_writer` by the reader browsers and never shown as content. Runs on T1 and T2.
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  inviteMember,
  joinAsNewUser,
  memberLog,
  memberRow,
  navigate,
  personOf,
  refusal,
  visit,
} from '../fixtures/identity.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import type { PageUser } from '../../../web/e2e/helpers/user.ts';
import { b64uEncode } from '../../../web/src/core/encoding/b64u';
import { bodyJson } from '../../../web/src/core/encoding/json';
import { uuid7 } from '../../../web/src/core/encoding/ids';
import { formatTime } from '../../../web/src/core/encoding/time';
import { generateCode, inviteSecrets, sealPackage } from '../../../web/src/core/crypto/invite';
import { newHeader, sealEvent } from '../../../web/src/core/crypto/event';
import { buildEntries } from '../../../web/src/core/log/entries';
import { epochKey, WG } from '../../../web/src/core/log/state';
import type { VerifiedLog } from '../../../web/src/core/log/verify';

const WG_NAME = 'Research kz18-wg-h4j6k8';
const FORGED_TEXT = 'kz18-forged-l2m4n6';

/** An event of `author` in the workgroup `wg`, sealed with the author's own key of the workgroup. */
async function wgEvent(
  author: PageUser,
  log: VerifiedLog,
  org: string,
  wg: string,
  fields: { kind: string; task?: string; ref?: string },
  body: unknown,
): Promise<unknown> {
  const keys = await author.keys(org, log);
  const epoch = log.state.epoch(WG, wg);
  const header = newHeader({
    id: uuid7(),
    org,
    scope: WG,
    scope_id: wg,
    epoch,
    author: author.userId,
    kind: fields.kind,
    ts: formatTime(Date.now()),
    lh: log.head.seq,
    wg,
    task: fields.task ?? '',
    ref: fields.ref ?? '',
  });
  return sealEvent(keys.get(epochKey(WG, wg, epoch))!, header, bodyJson(body), author.identity.sig.privateKey);
}

test('@E2E-18 @R-5.4 @R-8.1 @R-8.2 @R-8.3 @R-8.4 @R-8.5 @R-8.6 @R-11.3 @R-P.5 rights hold in the backend and the user interface, forged author events are flagged', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Admin');
  const org = ada.onboarded.org;

  // A further workgroup: the creating admin has write.
  await navigate(page, `/o/${org}/workgroups`);
  await page.getByTestId('workgroups.create.open').click();
  await page.getByTestId('workgroups.create.name').fill(WG_NAME);
  await page.getByTestId('workgroups.create.confirm').click();
  const research = (await page.getByTestId('workgroups.item').filter({ hasText: WG_NAME }).getAttribute('data-wg'))!;

  // A member and a second admin join (persons added at org level, R-8.1).
  const bob = await joinAsNewUser(browser, e2e, await inviteMember(page, org, { role: 'member' }), 'Bob Member');
  const carl = await joinAsNewUser(browser, e2e, await inviteMember(page, org, { role: 'admin' }), 'Carl Admin');

  // Ada grants Bob read access on the further workgroup (a person added at workgroup level, R-8.2).
  const bobRow = memberRow(page, 'Bob Member');
  await bobRow.getByTestId('members.item.grant').click();
  await page.getByTestId('members.grant.wg').selectOption(research);
  await page.getByTestId('members.grant.access').selectOption('read');
  await page.getByTestId('members.grant.confirm').click();
  await expect(bobRow.getByTestId('members.item.access')).toContainText('Research');

  let log = await ada.user.log(org);
  expect(log.state.member(bob.user.userId)).toMatchObject({ role: 'member' });
  expect(log.state.member(carl.user.userId)).toMatchObject({ role: 'admin' });
  expect(log.state.wgAccess(ada.user.userId, research)).toBe('write');
  expect(log.state.wgAccess(bob.user.userId, research)).toBe('read');
  // an admin reads every workgroup by inheritance (ADR-058); wg_create gives only the creator write
  expect(log.state.wgAccess(carl.user.userId, research)).toBe('read');
  expect(log.state.inherited(carl.user.userId, research)).toBe(true);
  expect(log.state.inherited(ada.user.userId, research)).toBe(false);
  // the default workgroup: write for every member (R-8.4)
  for (const user of [ada.user, bob.user, carl.user])
    expect(log.state.wgAccess(user.userId, log.state.defaultWg)).toBe('write');

  // The UI of the member: no admin controls, the further workgroup read-only.
  await visit(bob.page, `/o/${org}/members`);
  await expect(memberRow(bob.page, 'Bob Member').getByTestId('members.item.role')).toHaveText('Member');
  for (const id of [
    'members.invite.open',
    'members.invites',
    'members.item.role-change',
    'members.item.grant',
    'members.item.remove',
    'members.item.ungrant',
  ])
    await expect(bob.page.getByTestId(id)).toHaveCount(0);
  await navigate(bob.page, `/o/${org}/workgroups`);
  await expect(
    bob.page.getByTestId('workgroups.item').filter({ hasText: WG_NAME }).getByTestId('workgroups.item.access'),
  ).toHaveText('Read');
  for (const id of ['workgroups.create.open', 'workgroups.item.rename', 'workgroups.item.archive'])
    await expect(bob.page.getByTestId(id)).toHaveCount(0);
  await navigate(bob.page, `/o/${org}`);
  await expect(bob.page.getByTestId('home.wg').locator('option')).toHaveCount(1);
  await expect(bob.page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(0);
  await navigate(bob.page, `/o/${org}/w/${research}/whiteboard`);
  await expect(bob.page.getByTestId('whiteboard.empty')).toBeVisible();
  await expect(bob.page.getByTestId('whiteboard.create')).toHaveCount(0);
  await navigate(bob.page, `/o/${org}/devices`);
  await expect(bob.page.getByTestId('devices.screen')).toBeVisible();
  await expect(bob.page.getByTestId('devices.list')).toBeAttached();
  await expect(bob.page.getByTestId('devices.add.open')).toHaveCount(0);
  await expect(bob.page.getByTestId('devices.assign.open')).toHaveCount(0);
  // The second admin manages members and reads the workgroup, but does not get it in the composer (write only).
  await visit(carl.page, `/o/${org}/workgroups`);
  await expect(
    carl.page.getByTestId('workgroups.item').filter({ hasText: WG_NAME }).getByTestId('workgroups.item.access'),
  ).toHaveText('Read');
  await navigate(carl.page, `/o/${org}/w/${research}/whiteboard`);
  await expect(carl.page.getByTestId('whiteboard.empty')).toBeVisible({ timeout: 60_000 });
  await expect(carl.page.getByTestId('whiteboard.create')).toHaveCount(0);
  await navigate(carl.page, `/o/${org}`);
  await expect(carl.page.getByTestId('home.wg').locator('option')).toHaveCount(1);
  await expect(carl.page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(0);
  await navigate(carl.page, `/o/${org}/members`);
  await expect(carl.page.getByTestId('members.invite.open')).toBeVisible();

  // The backend: direct API calls with the member's session for what it may not do.
  const bobLog = await memberLog(bob.user, org);
  const secrets = await inviteSecrets(generateCode());
  const [inviteEntry] = await buildEntries(
    { org, head: bobLog.head },
    { id: bob.user.userId, key: bob.user.identity.sig.privateKey },
    [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'user',
        role: 'admin',
        wgs: {},
        expires: formatTime(Date.now() + 3600_000),
      },
    ],
    formatTime(Date.now()),
  );
  // a well-formed package (the hub checks the body before the right): the member's own keys of the default workgroup
  const bobKeys = await bob.user.keys(org, bobLog);
  const defaultEpoch = bobLog.state.epoch(WG, bobLog.state.defaultWg);
  await anonymousBudget(COST.page);
  const config = (await (await bob.page.request.get('/v1/config')).json()) as {
    hub_key: { id: string; public_key: string };
  };
  const packageCt = await sealPackage(secrets, {
    org,
    org_name: 'Forged',
    wg_names: { [bobLog.state.defaultWg]: 'Default' },
    founder: bobLog.state.founder,
    head: bobLog.head,
    keys: [
      {
        scope: WG,
        scope_id: bobLog.state.defaultWg,
        epoch: defaultEpoch,
        key: bobKeys.get(epochKey(WG, bobLog.state.defaultWg, defaultEpoch))!.reveal(b64uEncode),
      },
    ],
    hub_key: config.hub_key,
    v: 1,
  });
  expect(
    await refusal(bob.user, 'POST', `/v1/orgs/${org}/invites`, { entry: inviteEntry, package_ct: packageCt }),
  ).toBe('403 forbidden');
  // reads without the right are `404 not_found` (the hub does not tell what exists, rights rule of HUB-02)
  expect(await refusal(bob.user, 'GET', `/v1/orgs/${org}/invites`)).toBe('404 not_found');
  expect(await refusal(bob.user, 'GET', `/v1/workgroups/${research}/device-candidates`)).toBe('404 not_found');
  const task = uuid7();
  const events = [
    await wgEvent(bob.user, bobLog, org, research, { kind: 'task.meta', task }, { title: 'not mine' }),
    await wgEvent(bob.user, bobLog, org, research, { kind: 'prompt', task }, { text: 'hi', attachments: [] }),
  ];
  expect(await refusal(bob.user, 'POST', '/v1/tasks', { task_id: task, wg_id: research, events })).toBe(
    '403 forbidden',
  );
  // the member's log entries that need admin rights are refused as such (log_rights)
  const roleChange = await buildEntries(
    { org, head: bobLog.head },
    { id: bob.user.userId, key: bob.user.identity.sig.privateKey },
    [{ type: 'role', subject: bob.user.userId, role: 'admin' }],
    formatTime(Date.now()),
  );
  expect(
    await refusal(bob.user, 'POST', `/v1/orgs/${org}/log`, {
      expected_seq: bobLog.head.seq + 1,
      entries: roleChange,
      wraps: [],
      events: [],
    }),
  ).toBe('400 log_rights');
  // a whiteboard entry in the workgroup the member only reads: refused by the hub's event check (E5)
  const entry = await wgEvent(
    bob.user,
    bobLog,
    org,
    research,
    { kind: 'whiteboard.entry', ref: uuid7() },
    { level: 'workgroup', text: FORGED_TEXT },
  );
  expect(await refusal(bob.user, 'POST', '/v1/events', { events: [{ event: entry }] })).toBe('400 author_not_writer');
  // the second admin reads the workgroup by inheritance but writes nothing in it without a `grant`
  expect(await refusal(carl.user, 'GET', `/v1/scopes/${research}/events`)).toBe('ok');
  expect(await refusal(carl.user, 'GET', `/v1/workgroups/${research}/device-candidates`)).toBe('404 not_found');
  const carlLog = await memberLog(carl.user, org);
  // (the key reaches the second admin by catch-up from the creating admin's browser)
  await expect
    .poll(async () => (await carl.user.keys(org, carlLog)).has(epochKey(WG, research, 1)), { timeout: 60_000 })
    .toBe(true);
  const carlTask = uuid7();
  expect(
    await refusal(carl.user, 'POST', '/v1/tasks', {
      task_id: carlTask,
      wg_id: research,
      events: [
        await wgEvent(carl.user, carlLog, org, research, { kind: 'task.meta', task: carlTask }, { title: 'not mine' }),
        await wgEvent(
          carl.user,
          carlLog,
          org,
          research,
          { kind: 'prompt', task: carlTask },
          { text: 'hi', attachments: [] },
        ),
      ],
    }),
  ).toBe('403 forbidden');

  // A tampered hub stores the refused event anyway: the reader browsers flag it and never show its text.
  const stored = await orchestrator.call<{ scope_seq: number }>('POST', '/hub-db/forge-event', {
    event: entry,
    author_kind: 'user',
  });
  expect(stored.scope_seq).toBeGreaterThan(0);
  // (a tampered insert sends no live frame: the readers load the board afresh)
  for (const reader of [page, bob.page, carl.page]) {
    await visit(reader, `/o/${org}/w/${research}/whiteboard`);
    const flagged = reader.locator('[data-testid="whiteboard.item"][data-type="unverifiable"]');
    await expect(flagged).toHaveAttribute('data-code', 'author_not_writer', { timeout: 30_000 });
    await expect(reader.getByText(FORGED_TEXT)).toHaveCount(0);
  }
  log = await ada.user.log(org);
  expect(log.state.wgAccess(bob.user.userId, research)).toBe('read');
  await expectNoPlaintext(orchestrator, ['kz18-wg-h4j6k8', FORGED_TEXT]);
  await bob.context.close();
  await carl.context.close();
  await closingChecks(orchestrator);
});
