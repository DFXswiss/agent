// E2E-04 (07-testing.md section 2.1; 04-crypto.md sections 3, 5.8, 5.10; 05-runner.md sections 1.2, 2.1; 03-api.md
// section 6): an org device. The admin creates a device invitation on the device screen; a further runner instance on
// the runner device enrolls with `agent-runner enroll --code` and authenticates with its device key (signed requests,
// signed WebSocket: active and connected at the hub); a task of the org is scheduled on it and gets a VM of the
// workgroup there (without a credential of the owner the session then ends with `model_access_unavailable`: an org
// device never uses the platform model key, 05-runner.md section 10.1; credentials are M2b, E2E-27/28/36). The admin
// unpairs it: one batch with `remove` and the rotations, the hub refuses the device, the runner ends its sessions and
// deletes the workgroup's VM and its keys; there is no wrap of the new epochs for it. Negative: `enroll` with an
// invalid and with a used code (`invite_code_invalid`, `invite_unknown`); `agent-runner doctor` reports the host OS
// `darwin` as supported. Runs on T1 and T2.
import { expect, test } from '../fixtures/test.ts';
import {
  addDeviceInvitation,
  anonymousBudget,
  closingChecks,
  COST,
  enrollAttempt,
  deviceItem,
  enrollRunner,
  expectDevice,
  navigate,
  removeRunner,
  runnerCli,
  spawnRunner,
} from '../fixtures/identity.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import { home, mainSessions, onboard, sendPrompt, transitions, uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { ORG, WG } from '../../../web/src/core/log/state';

const INVALID_CODE = 'AAAAA-AAAAA-AAAAA-AAAAA-AAAAA1';

test('@E2E-04 @R-1.3 @R-1.8 @R-1.9 @R-1.12 @R-3.2 @R-3.5 @R-3.6 @R-P.5 @needs-vm an org device enrolls with a code, runs a task and is unpaired with rotation', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  await authenticator('platform');
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, 'Ada Own Device');
  const org = onboarded.org;
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const defaultWg = (await user.log(org)).state.defaultWg;
  const RUNNER = (await spawnRunner(orchestrator, { device: 'org', pool_size: 1 })).name;
  try {
    // The device screen creates the invitation (default workgroup) and shows code and command once.
    const code = await addDeviceInvitation(page, org, [defaultWg]);
    await expect(page.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'false');

    // Negative: an invalid code is refused before any request.
    const invalid = await enrollAttempt(orchestrator, RUNNER, INVALID_CODE);
    expect(invalid.rc).not.toBe(0);
    expect(`${invalid.stdout}${invalid.stderr}`).toContain('invite_code_invalid');

    // The device enrolls: active at once (org device), the screen follows live.
    const device = await enrollRunner(orchestrator, RUNNER, code);
    await expect(page.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'true', { timeout: 30_000 });
    await page.getByTestId('devices.add.done').click();
    await expect(deviceItem(page, device).getByTestId('devices.item.kind')).toHaveText('Own device');
    // signed requests and the signed WebSocket upgrade: active and connected
    await expectDevice(orchestrator, device, { state: 'active', online: true });
    await expect(deviceItem(page, device).getByTestId('devices.item.online')).toHaveText('Online', { timeout: 30_000 });
    const [row] = await orchestrator.hubQuery<{ kind: string; os: string; owner_org: string }>(
      `SELECT kind, os, owner_org::text FROM hub.device WHERE id = '${uuid(device)}'`,
    );
    expect(row).toEqual({ kind: 'org', os: 'darwin', owner_org: org });
    let log = await user.log(org);
    expect(log.state.assigned(device, defaultWg)).toBe(true);
    expect(log.entries.at(-1)!.body).toMatchObject({ type: 'accept', kind: 'device', subject: device });

    // `agent-runner doctor`: macOS is the supported host OS (R-3.6); its signed request to the hub is accepted.
    const doctor = await runnerCli(orchestrator, RUNNER, ['doctor']);
    expect(doctor.stdout).toMatch(/^runner\.doctor\.check name=os result=ok detail=supported value=darwin$/m);
    expect(doctor.stdout).toMatch(/^runner\.doctor\.check name=hub result=ok detail=pinned_key value=active$/m);

    // Negative: the used code enrolls nothing (a second instance, the first one is enrolled now).
    const SPARE = (await spawnRunner(orchestrator, { device: 'org', pool_size: 0 })).name;
    const used = await enrollAttempt(orchestrator, SPARE, code);
    expect(used.rc).not.toBe(0);
    expect(`${used.stdout}${used.stderr}`).toContain('invite_unknown');
    await removeRunner(orchestrator, SPARE);

    // A task of the org is scheduled on its own device and gets a VM of the workgroup there; without a credential of
    // the owner the device has no model access for it (no platform key on org devices).
    await home(page);
    const task = await sendPrompt(page, 'E2E04-ORGDEVICE kz04-prompt-x8z1a3: hello');
    await expect
      .poll(async () => (await mainSessions(orchestrator, task))[0]?.state, { timeout: 120_000, intervals: [1_000] })
      .toBe('failed');
    const [session] = await mainSessions(orchestrator, task);
    expect(session).toMatchObject({ device_id: device });
    const [reason] = await orchestrator.hubQuery<{ state_reason: string }>(
      `SELECT state_reason FROM hub.session WHERE id = '${uuid(session!.id)}'`,
    );
    expect(reason).toEqual({ state_reason: 'model_access_unavailable' });
    expect(await transitions(orchestrator, session!.id)).toEqual(
      expect.arrayContaining(['requested', 'vm_assigning', 'vm_ready', 'failed']),
    );
    await expect
      .poll(
        async () =>
          runnerStore<{ n: number }>(
            orchestrator,
            `SELECT count(*)::int AS n FROM runner.vm
          WHERE state <> 'deleted' AND wg_id = '${uuid(defaultWg)}'`,
            RUNNER,
          ),
        { timeout: 60_000 },
      )
      .toEqual([{ n: 1 }]);
    const keysBefore = await runnerStore<{ n: number }>(
      orchestrator,
      'SELECT count(*)::int AS n FROM runner.scope_key',
      RUNNER,
    );
    expect(keysBefore[0]!.n).toBeGreaterThanOrEqual(2);

    // Unpairing on the device screen: remove + rotations in one batch; the device is gone from the list.
    const before = log.entries.length;
    const orgEpoch = log.state.epoch(ORG, org);
    const wgEpoch = log.state.epoch(WG, defaultWg);
    await navigate(page, `/o/${org}/devices`);
    await deviceItem(page, device).getByTestId('devices.item.unpair').click();
    await page.getByTestId('devices.unpair.confirm').click();
    await expect(deviceItem(page, device)).toHaveCount(0, { timeout: 30_000 });
    log = await user.log(org);
    const batch = log.entries.slice(before).map((entry) => entry.body.type);
    expect(batch[0]).toBe('remove');
    expect(batch.slice(1).every((type) => type === 'rotate')).toBe(true);
    expect(log.state.member(device)).toBeNull();
    expect(log.state.epoch(ORG, org)).toBe(orgEpoch + 1);
    expect(log.state.epoch(WG, defaultWg)).toBe(wgEpoch + 1);
    // no wrap of the org's keys for the device any more (old ones deleted, none of the new epochs); the hub refuses
    // its signed requests (no member of the org any more – the device row itself stays as enrolled –, connection
    // closed, `agent-runner doctor`'s signed request refused with a signed `key_revoked`)
    const wraps = await orchestrator.hubQuery<{ scope: string; epoch: number }>(
      `SELECT scope, epoch FROM hub.key_wrap WHERE recipient_id = '${uuid(device)}' AND scope IN ('org', 'wg')`,
    );
    expect(wraps).toEqual([]);
    await expectDevice(orchestrator, device, { online: false });
    const refused = await runnerCli(orchestrator, RUNNER, ['doctor']);
    expect(refused.stdout).toMatch(/^runner\.doctor\.check name=hub result=fail detail=key_revoked$/m);

    // The runner learns it was unpaired: sessions end, the workgroup's VM and the keys are deleted.
    await expect
      .poll(async () => (await runnerCli(orchestrator, RUNNER, ['status'])).stdout, {
        timeout: 240_000,
        intervals: [5_000],
      })
      .toMatch(/^runner\.status\.hub .*revoked=key_revoked\b/m);
    await expect
      .poll(
        async () =>
          runnerStore<{ vms: number; keys: number }>(
            orchestrator,
            `SELECT (SELECT count(*)::int FROM runner.vm WHERE state <> 'deleted' AND wg_id IS NOT NULL) AS vms,
                    (SELECT count(*)::int FROM runner.scope_key) AS keys`,
            RUNNER,
          ),
        { timeout: 120_000, intervals: [2_000] },
      )
      .toEqual([{ vms: 0, keys: 0 }]);
    await expectNoPlaintext(orchestrator, ['kz04-prompt-x8z1a3']);
  } finally {
    await removeRunner(orchestrator, RUNNER).catch(() => undefined);
  }
  await closingChecks(orchestrator);
});
