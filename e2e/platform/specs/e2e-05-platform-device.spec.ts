// E2E-05 (07-testing.md section 2.1; 04-crypto.md sections 4.8, 5.9, 5.10; 03-api.md sections 4.3, 4.6.1, 6;
// ADR-023, ADR-026): a platform device. The operator invites a device (`invite-device`), a further runner instance
// enrolls with `enroll --platform` (pending) and the operator certifies it (`certify-pending`). A new user's first
// prompt makes the browser assign it: `device_assign` signed by the user with the certification's `platform_seq`
// (verified against the compiled root key), wraps of the org and workgroup keys for the device; the task runs there.
// Decertification: the operator appends `device_decertify`; the hub refuses the device and marks the org
// `rotation_pending`; the admin's browser sends `device_unassign` with the rotations by itself; the device holds no
// key of the new epochs. Entitlement: a non-private org gets `platform_access_denied` on `device_assign` until the
// operator appends `org_platform_access`. Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  enrollRunner,
  expectDevice,
  navigate,
  refusal,
  removeRunner,
  runnerCli,
  spawnRunner,
  visit,
} from '../fixtures/identity.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  invite,
  mainSessions,
  onboard,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { formatTime } from '../../../web/src/core/encoding/time';
import { buildBatch } from '../../../web/src/core/log/rotation';
import { epochKey, ORG, WG } from '../../../web/src/core/log/state';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-05-platform-device.json', import.meta.url), 'utf8'),
);

interface Capacity {
  readonly id: string;
  readonly free: number | null;
}

/** A `device_assign` batch of a platform device for the org's default workgroup, as the composer builds it. */
async function assignBatch(user: PageUser, org: string, device: string, platformSeq: number): Promise<unknown> {
  const log = await user.log(org);
  const keys = await user.keys(org, log);
  const built = await buildBatch({
    log,
    author: { id: user.userId, identity: user.identity },
    entries: [{ type: 'device_assign', device, wg: log.state.defaultWg, platform_seq: platformSeq }],
    keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
    platform: await user.platform(),
    ts: formatTime(Date.now()),
  });
  return built.body;
}

test('@E2E-05 @R-0.5 @R-1.3 @R-1.12 @R-3.4 @R-5.5 @R-P.5 @R-P.12 @needs-vm a certified platform device is assigned automatically, decertified with rotation, and only for entitled orgs', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(720_000);
  await orchestrator.loadScenario('model', scenario);
  // The run's platform device (runner-1) for the entitlement step; the new one gets more free capacity (no memory
  // reserve), so the browser's first candidate is the new device (03-api.md section 4.5: online, then free slots).
  const shared = await platformDevice(orchestrator);
  const RUNNER = (await spawnRunner(orchestrator, { pool_size: 1, memory_reserve_mib: 0 })).name;
  try {
    // Operator invitation, platform enrollment (pending), certification.
    const invitation = await invite(orchestrator, 'device');
    const device = await enrollRunner(orchestrator, RUNNER, invitation.code, true);
    await expectDevice(orchestrator, device, { state: 'pending_certification' });
    expect(await operator(orchestrator, ['certify-pending', '--device', device])).toContain(`certified ${device}`);
    await expectDevice(orchestrator, device, { state: 'active', online: true });
    await poolReady(orchestrator, 1);
    await expect
      .poll(
        async () => {
          const rows = await orchestrator.hubQuery<Capacity>(
            `SELECT id, (capacity->>'vm_slots_free')::int AS free FROM hub.device
              WHERE id IN ('${uuid(device)}', '${uuid(shared)}')`,
          );
          const free = (id: string) => rows.find((row) => row.id === id)?.free ?? -1;
          return free(device) > free(shared);
        },
        { timeout: 60_000 },
      )
      .toBe(true);

    // A new user's first prompt: the browser assigns the certified device and the task runs on it.
    await authenticator('platform');
    await anonymousBudget(COST.register);
    const onboarded = await onboard(page, orchestrator, 'Ada Platform');
    const org = onboarded.org;
    const user = await PageUser.open(page, onboarded.recoveryCode);
    const task = await sendPrompt(page, 'E2E05-PLATFORM kz05-prompt-e4r6t8: hello');
    await expectAnswerInChat(page, 'Platform device kz05-answer-r7t9y2.');
    const [session] = await mainSessions(orchestrator, task);
    expect(session).toMatchObject({ device_id: device });
    let log = await user.log(org);
    const wg = log.state.defaultWg;
    const assign = log.entries.find((entry) => entry.body.type === 'device_assign')!;
    expect(assign.body).toMatchObject({ device, wg, by: user.userId });
    // the certification the user's browser verified against the compiled root key names this device and its keys
    const platform = await user.platform();
    const certification = platform.certification(assign.body.platform_seq as number)!;
    expect(certification.device).toBe(device);
    expect(platform.valid(assign.body.platform_seq as number)).toBe(true);
    const wraps = await orchestrator.hubQuery<{ scope: string; epoch: number; wrapped_by: string }>(
      `SELECT scope, epoch, wrapped_by::text FROM hub.key_wrap WHERE recipient_id = '${uuid(device)}' ORDER BY scope`,
    );
    // the user's credential key too (04-crypto.md section 5.9: the device may use the owner's credentials)
    expect(wraps).toEqual([
      { scope: 'cred', epoch: 1, wrapped_by: user.userId },
      { scope: 'org', epoch: 1, wrapped_by: user.userId },
      { scope: 'wg', epoch: 1, wrapped_by: user.userId },
    ]);
    // the device holds the keys of its assignment (R-5.5)
    const held = await runnerStore<{ scope: string; epoch: number }>(
      orchestrator,
      `SELECT scope, epoch FROM runner.scope_key WHERE scope IN ('org', 'wg') ORDER BY scope`,
      RUNNER,
    );
    expect(held).toEqual([
      { scope: 'org', epoch: 1 },
      { scope: 'wg', epoch: 1 },
    ]);

    // Decertification while the admin's browser is cut off: the hub refuses the device, the org owes a rotation.
    await orchestrator.cut('browser-hub');
    expect(await operator(orchestrator, ['decertify', '--device', device])).toContain('appended device_decertify');
    await expectDevice(orchestrator, device, { state: 'decertified', online: false });
    // the hub refuses the device's signed requests with `key_revoked` (`agent-runner doctor` asks for its state)
    const doctor = await runnerCli(orchestrator, RUNNER, ['doctor']);
    expect(doctor.stdout).toMatch(/^runner\.doctor\.check name=hub result=fail detail=key_revoked$/m);
    const [pending] = await orchestrator.hubQuery<{ pending: string }>(
      `SELECT rotation_pending::text AS pending FROM hub.org WHERE id = '${uuid(org)}'`,
    );
    expect(pending!.pending).toContain(wg);
    // The admin's browser comes back and sends device_unassign with the rotations by itself.
    await orchestrator.restore('browser-hub');
    await visit(page, `/o/${org}`);
    await expect
      .poll(async () => (await user.log(org)).entries.some((entry) => entry.body.type === 'device_unassign'), {
        timeout: 120_000,
        intervals: [2_000],
      })
      .toBe(true);
    log = await user.log(org);
    const tail = log.entries.slice(log.entries.findIndex((entry) => entry.body.type === 'device_unassign'));
    expect(tail.map((entry) => entry.body.type)).toEqual(['device_unassign', 'rotate', 'rotate']);
    expect(tail[0]!.body).toMatchObject({ device, wg, by: user.userId });
    expect([log.state.epoch(ORG, org), log.state.epoch(WG, wg)]).toEqual([2, 2]);
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ pending: string }>(
              `SELECT rotation_pending::text AS pending FROM hub.org WHERE id = '${uuid(org)}'`,
            )
          )[0]!.pending,
        { timeout: 30_000 },
      )
      .toBe('[]');
    // no key of the new epochs for the device: no wrap at the hub, none in its store
    const newer = await orchestrator.hubQuery<{ n: number }>(
      `SELECT count(*)::int AS n FROM hub.key_wrap WHERE recipient_id = '${uuid(device)}' AND epoch >= 2`,
    );
    expect(newer).toEqual([{ n: 0 }]);
    const after = await runnerStore<{ n: number }>(
      orchestrator,
      `SELECT count(*)::int AS n FROM runner.scope_key WHERE epoch >= 2`,
      RUNNER,
    );
    expect(after).toEqual([{ n: 0 }]);
    // the decertified device is no candidate any more
    const candidates = (await user.request('GET', `/v1/workgroups/${wg}/device-candidates`)) as {
      candidates: { device_id: string }[];
    };
    expect(candidates.candidates.map((item) => item.device_id)).not.toContain(device);

    // Entitlement: a non-private org of the same user may not assign a platform device until the operator grants it.
    await page.getByTestId('org-switcher.current').click();
    await page.getByTestId('org-switcher.create.open').click();
    await page.getByTestId('org-switcher.create.name').fill('Acme kz05-org-a1s3');
    await page.getByTestId('org-switcher.create.confirm').click();
    await expect(page.getByTestId('org-switcher.current')).toHaveText('Acme kz05-org-a1s3');
    const acme = /\/o\/([0-9a-f-]{36})$/.exec(page.url())![1]!;
    await navigate(page, `/o/${acme}/devices`);
    await expect(page.getByTestId('devices.platform.denied')).toBeVisible();
    const [cert] = await orchestrator.hubQuery<{ seq: number }>(
      `SELECT certified_seq AS seq FROM hub.device WHERE id = '${uuid(shared)}'`,
    );
    expect(await refusal(user, 'POST', `/v1/orgs/${acme}/log`, await assignBatch(user, acme, shared, cert!.seq))).toBe(
      '403 platform_access_denied',
    );
    expect(await operator(orchestrator, ['grant-org-access', '--org', acme])).toContain('appended org_platform_access');
    expect(await refusal(user, 'POST', `/v1/orgs/${acme}/log`, await assignBatch(user, acme, shared, cert!.seq))).toBe(
      'ok',
    );
    expect((await user.log(acme)).state.assigned(shared, (await user.log(acme)).state.defaultWg)).toBe(true);
    await visit(page, `/o/${acme}/devices`);
    await expect(page.getByTestId('devices.platform.denied')).toHaveCount(0);
    await expect(page.getByTestId('devices.assign.open')).toBeVisible();
    await expectNoPlaintext(orchestrator, ['kz05-prompt-e4r6t8', 'kz05-answer-r7t9y2', 'kz05-org-a1s3']);
  } finally {
    await orchestrator.restore('browser-hub').catch(() => undefined);
    await removeRunner(orchestrator, RUNNER).catch(() => undefined);
  }
  await closingChecks(orchestrator, true);
});
