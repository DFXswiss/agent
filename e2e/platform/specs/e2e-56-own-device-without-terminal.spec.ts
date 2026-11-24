// E2E-56 (ADR-074; 05-runner.md section 16; 11-agent-access.md section 8.4; KOR-59): the user's agent connects
// the user's own Mac as an execution device with one command. Ada pairs her agent; the hub's download area offers a
// signed test release (orchestrator/routes/own_device.py). `agent-platform device add --this-computer` checks the Mac,
// verifies the feed and the package with the release key, creates the device invitation of the default workgroup and
// runs the one administrator step – on the test devices a stand-in that gets the very arguments of the root script and
// enrolls a second runner instance with the code of the request file, as `postinstall` does (no root there; the root
// helper itself is proven in a fresh macOS guest, manual-checks/own-device.md). The command reports the device, the
// dialog sentence and the plaintext notice of A7; the device screen offers the package for a Mac. The device is an active, connected own device of the org; a task of
// the org is scheduled on it and gets a VM of the workgroup there (without a credential of the owner it ends with
// `model_access_unavailable`, as in E2E-04). `device add` again is `device_installed`. `device remove --this-computer`
// unpairs the device (`remove` plus rotations) and prints the helper's answer (the stand-in's here); the hub refuses the device and the runner
// ends its sessions and deletes the workgroup's VM. Runs on T1.
import { expect, test } from '../fixtures/test.ts';
import { deviceItem, expectDevice, removeRunner, spawnRunner, visit } from '../fixtures/identity.ts';
import { agentTool, agentToolRun, pairAgent } from '../fixtures/agent-tool.ts';
import { runnerStore } from '../fixtures/guest.ts';
import { home, mainSessions, onboard, sendPrompt, transitions, uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const HOME = 'mac';

test('@E2E-56 @R-3.14 @R-3.5 @needs-vm an agent connects its own Mac with one command, a task runs there, removal unpairs', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada Own Mac');
  const org = onboarded.org;
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const defaultWg = (await user.log(org)).state.defaultWg;
  expect(await pairAgent(orchestrator, page, e2e.hubOrigin, HOME)).toMatchObject({ ok: true });
  const RUNNER = (await spawnRunner(orchestrator, { device: 'org', pool_size: 0 })).name;
  try {
    const release = await orchestrator.call<{ key: string; version: string; package: string }>(
      'POST',
      '/own-device/prepare',
      { home: HOME, runner: RUNNER },
    );

    // One command: checks, verified release, invitation, the administrator step, the enrollment.
    const run = await agentToolRun(orchestrator, HOME, ['device', 'add', '--this-computer'], 240_000);
    expect(run.rc, `device add: ${JSON.stringify(run.result)} ${run.stderr_tail}`).toBe(0);
    const added = run.result ?? {};
    const device = added.device_id as string;
    expect(device).toMatch(/^[0-9a-f-]{36}$/);
    expect(added).toMatchObject({
      ok: true,
      org_id: org,
      workgroups: [defaultWg],
      version: release.version,
      dialog: 'macOS asks once for your password: this installs the DFX execution service on this Mac.',
    });
    expect(added.notice as string).toContain('Whoever administers this Mac can read the content of every task');
    expect((added.checks as { name: string; ok: boolean }[]).every((check) => check.ok)).toBe(true);
    expect(JSON.stringify(added)).not.toMatch(/[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{6}/);

    // The device is an own device of the org, assigned to the default workgroup, active and connected.
    await orchestrator.call('POST', `/runners/${RUNNER}/start`);
    await expectDevice(orchestrator, device, { state: 'active', online: true });
    const log = await user.log(org);
    expect(log.state.assigned(device, defaultWg)).toBe(true);
    expect(log.entries.at(-1)!.body).toMatchObject({ type: 'accept', kind: 'device', subject: device });
    // the pairing left the page at the sign-in (agent-tool.ts): sign in again with the passkey
    await visit(page, `/o/${org}/devices`);
    await expect(deviceItem(page, device).getByTestId('devices.item.kind')).toHaveText('Own device');

    // Without an agent: the device screen offers the notarised package of the release next to a new code.
    await page.getByTestId('devices.add.open').click();
    await page.getByTestId('devices.add.submit').click();
    await expect(page.getByTestId('devices.add.mac-download')).toHaveAttribute(
      'href',
      `/downloads/runner/${release.package}`,
    );
    await page.getByTestId('devices.add.done').click();

    // A second `device add` on the same Mac is refused: one device is one identity.
    const again = await agentToolRun(orchestrator, HOME, ['device', 'add', '--this-computer']);
    expect(again.rc).not.toBe(0);
    expect(again.result).toMatchObject({ ok: false, error: 'device_installed', device_id: device });

    // A task of the org is scheduled on the own device and gets a VM of the workgroup there.
    await home(page);
    const task = await sendPrompt(page, 'E2E51-OWNMAC kz51-prompt-m4n6b8: hello');
    await expect
      .poll(async () => (await mainSessions(orchestrator, task))[0]?.state, { timeout: 180_000, intervals: [1_000] })
      .toBe('failed');
    const [session] = await mainSessions(orchestrator, task);
    expect(session).toMatchObject({ device_id: device });
    expect(await transitions(orchestrator, session!.id)).toEqual(
      expect.arrayContaining(['requested', 'vm_assigning', 'vm_ready', 'failed']),
    );
    const [reason] = await orchestrator.hubQuery<{ state_reason: string }>(
      `SELECT state_reason FROM hub.session WHERE id = '${uuid(session!.id)}'`,
    );
    expect(reason).toEqual({ state_reason: 'model_access_unavailable' });

    // Removal: unpaired in the log (remove plus rotations), nothing left of the installation.
    const before = (await user.log(org)).entries.length;
    const removed = await agentTool(orchestrator, HOME, ['device', 'remove', '--this-computer'], 120_000);
    expect(removed).toMatchObject({ ok: true, device_id: device, unpaired: true, leftovers: [] });
    const after = await user.log(org);
    const batch = after.entries.slice(before).map((entry) => entry.body.type);
    expect(batch[0]).toBe('remove');
    expect(batch.slice(1).every((type) => type === 'rotate')).toBe(true);
    expect(after.state.member(device)).toBeNull();
    await expectDevice(orchestrator, device, { online: false });
    await expect
      .poll(
        async () =>
          runnerStore<{ n: number }>(
            orchestrator,
            `SELECT count(*)::int AS n FROM runner.vm WHERE state <> 'deleted'`,
            RUNNER,
          ),
        { timeout: 120_000 },
      )
      .toEqual([{ n: 0 }]);
    const status = await agentToolRun(orchestrator, HOME, ['device', 'remove', '--this-computer']);
    expect(status.result).toMatchObject({ ok: false, error: 'not_installed' });
  } finally {
    await removeRunner(orchestrator, RUNNER);
  }
});
