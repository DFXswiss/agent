// U-WEB-core-vm-switch (KOR-70, ADR-093 sections 3 and 5): the switch status of an outdated workgroup VM as
// `GET /v1/workgroups/{wg}` reports it, the outdated VMs of the devices, the status in words and the request by hand.
import { describe, expect, it } from 'vitest';
import { actionErrorCode } from '../../../src/core/org/batch';
import { HubError } from '../../../src/core/net/signedFetch';
import { loadWorkgroupDevices } from '../../../src/core/tasks/placement';
import {
  HOLD_REASONS,
  outdatedVms,
  requestVmSwitch,
  shortId,
  switchStatus,
  upgradeOf,
  type VmUpgrade,
} from '../../../src/core/tasks/vmswitch';
import { taskWorld } from './world';

const SINCE = '2026-11-25T10:00:00.000Z';
const S1 = '01930000-0000-7000-8000-0000000000a1';
const S2 = '01930000-0000-7000-8000-0000000000b2';

const UPGRADE = {
  golden_version: 'g2',
  golden_built_at: SINCE,
  state: 'waiting',
  holds: [{ reason: 'session_active', session_id: S1, since: SINCE }],
  sessions: [S2],
};

async function refusal(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return actionErrorCode(error);
  }
  throw new Error('not refused');
}

describe('upgradeOf', () => {
  it('reads a status and drops malformed holds and sessions', () => {
    expect(upgradeOf(UPGRADE)).toEqual({
      golden_version: 'g2',
      golden_built_at: SINCE,
      state: 'waiting',
      holds: [{ reason: 'session_active', session_id: S1, since: SINCE }],
      sessions: [S2],
    });
    expect(
      upgradeOf({
        golden_version: 'g3',
        golden_built_at: 5,
        state: 'switching',
        holds: [{ reason: 'capacity', since: SINCE }, { reason: 'later_reason', since: SINCE }, { reason: 'x' }, null],
        sessions: [S1, 7],
      }),
    ).toEqual({
      golden_version: 'g3',
      golden_built_at: null,
      state: 'switching',
      // a reason this client does not know still holds the VM
      holds: [
        { reason: 'capacity', session_id: null, since: SINCE },
        { reason: 'unknown', session_id: null, since: SINCE },
      ],
      sessions: [S1],
    });
    expect(upgradeOf({ golden_version: 'g', state: 'waiting', holds: 'x', sessions: 'y' })).toEqual({
      golden_version: 'g',
      golden_built_at: null,
      state: 'waiting',
      holds: [],
      sessions: [],
    });
  });

  it('is null when absent or malformed', () => {
    for (const value of [null, undefined, 'x', {}, { golden_version: 1, state: 'waiting' }, { golden_version: 'g' }]) {
      expect(upgradeOf(value)).toBeNull();
    }
    expect(upgradeOf({ golden_version: 'g', state: 'done' })).toBeNull();
  });

  it('knows every hold reason of the contract', () => {
    expect([...HOLD_REASONS]).toEqual([
      'session_active',
      'subagent',
      'checkpoint_due',
      'checkpoint_missing',
      'workspace_incomplete',
      'capacity',
      'switch_failed',
    ]);
  });
});

describe('the outdated VMs of a workgroup', () => {
  it('lists the VMs with a status in device and VM order and words their status', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({
      devices: [
        {
          device_id: 'd1',
          vms: [
            { vm_id: 'vm-bbbbbbbb', state: 'stopped', sessions: 0, upgrade: UPGRADE, golden_version: 'g1' },
            { vm_id: 'vm-aaaaaaaa', state: 'running', sessions: 1, upgrade: null },
            { vm_id: 'vm-cccccccc', state: 'running', sessions: 1, upgrade: { state: 'waiting' } },
          ],
        },
        { device_id: 'd2', vms: [{ vm_id: 'vm-dddddddd', state: 'stopped', sessions: 0, upgrade: UPGRADE }] },
      ],
    }));
    const found = await loadWorkgroupDevices(t.context, t.wg);
    // a VM without a (valid) status carries no `upgrade` at all
    expect(found.devices[0]!.vms[0]).toEqual({ vm_id: 'vm-aaaaaaaa', state: 'running', sessions: 1 });
    expect(Object.hasOwn(found.devices[0]!.vms[0]!, 'upgrade')).toBe(false);
    const vms = outdatedVms(found.devices);
    expect(vms.map((vm) => [vm.device_id, vm.vm_id])).toEqual([
      ['d1', 'vm-bbbbbbbb'],
      ['d2', 'vm-dddddddd'],
    ]);
    expect(vms[0]!.upgrade.sessions).toEqual([S2]);
    expect(outdatedVms([])).toEqual([]);
  });

  it('words the status: switching, moves without holds, else held', () => {
    const upgrade = upgradeOf(UPGRADE) as VmUpgrade;
    expect(switchStatus(upgrade)).toBe('held');
    expect(switchStatus({ ...upgrade, holds: [] })).toBe('moves');
    expect(switchStatus({ ...upgrade, state: 'switching' })).toBe('switching');
    expect(shortId(S1)).toBe('000000a1');
  });
});

describe('the switch by hand', () => {
  it('posts the request and reads the preview; refusals and malformed answers fail', async () => {
    const t = await taskWorld();
    const route = `POST /v1/workgroups/${t.wg}/vms/vm-aaaaaaaa/switch`;
    let sent: unknown = null;
    t.client.on(route, (body) => {
      sent = body;
      return { vm_id: 'vm-aaaaaaaa', requested: true, requested_at: SINCE, upgrade: UPGRADE };
    });
    const answer = await requestVmSwitch(t.context, t.wg, 'vm-aaaaaaaa');
    expect(sent).toEqual({});
    expect(answer.requested).toBe(true);
    expect(answer.requested_at).toBe(SINCE);
    expect(answer.upgrade?.golden_version).toBe('g2');
    // a current VM: nothing requested
    t.client.on(route, () => ({ requested: false, requested_at: null, upgrade: null }));
    expect(await requestVmSwitch(t.context, t.wg, 'vm-aaaaaaaa')).toEqual({
      requested: false,
      requested_at: null,
      upgrade: null,
    });
    t.client.on(route, () => ({ requested: 'yes' }));
    expect(await refusal(requestVmSwitch(t.context, t.wg, 'vm-aaaaaaaa'))).toBe('unavailable');
    t.client.on(route, () => null);
    expect(await refusal(requestVmSwitch(t.context, t.wg, 'vm-aaaaaaaa'))).toBe('unavailable');
    t.client.on(route, () => {
      throw new HubError('forbidden', 403);
    });
    expect(await refusal(requestVmSwitch(t.context, t.wg, 'vm-aaaaaaaa'))).toBe('forbidden');
  });
});
