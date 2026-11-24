// U-WEB-ui-tasks (device and VM choice, ADR-075 sections 6 and 7): the devices of a workgroup with their VMs and the
// default placement, the two `PUT …/placement` routes, the composer's preselection and the placement helpers.
import { describe, expect, it } from 'vitest';
import { actionErrorCode } from '../../../src/core/org/batch';
import type { SessionRow, TaskRow } from '../../../src/core/store/slices/tasks';
import {
  applyAutomatic,
  loadWorkgroupDevices,
  PLACEMENT_REASONS,
  placementBody,
  placementOf,
  samePlacement,
  sessionVm,
  releaseTaskPlacement,
  setTaskPlacement,
  setWorkgroupPlacement,
  taskPlacement,
  validChoice,
} from '../../../src/core/tasks/placement';
import { sessionRow, taskRow, taskWorld } from './world';

const CAPACITY = {
  vm_memory_budget_mib: 8192,
  vm_memory_used_mib: 0,
  vm_slots_total: 8,
  vm_slots_free: 6,
  pool_ready: 1,
  sessions_per_vm: 16,
};

async function refusal(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return actionErrorCode(error);
  }
  throw new Error('not refused');
}

describe('the devices of a workgroup', () => {
  it('reads devices, VMs (sorted) and the default; drops malformed rows', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({
      devices: [
        {
          device_id: 'd1',
          kind: 'platform',
          online: true,
          capacity: CAPACITY,
          assigned_via: 'org',
          vms: [
            { vm_id: 'vm-bbbbbbbb', state: 'running', sessions: 2 },
            { vm_id: 'vm-aaaaaaaa', state: 'stopped', sessions: 0 },
            { vm_id: 'vm-cccccccc', state: 'running', sessions: -1 },
            { vm_id: 'vm-aaaaaaaa', state: 'running', sessions: 1 },
            null,
          ],
        },
        { device_id: 'd2' },
        { device_id: 3 },
        null,
      ],
      placement: { device_id: 'd1', vm_id: 'vm-aaaaaaaa' },
    }));
    const found = await loadWorkgroupDevices(t.context, t.wg);
    expect(found.placement).toEqual({ device_id: 'd1', vm_id: 'vm-aaaaaaaa' });
    expect(found.devices).toEqual([
      {
        device_id: 'd1',
        kind: 'platform',
        online: true,
        capacity: CAPACITY,
        assigned_via: 'org',
        vms: [
          { vm_id: 'vm-aaaaaaaa', state: 'stopped', sessions: 0 },
          { vm_id: 'vm-aaaaaaaa', state: 'running', sessions: 1 },
          { vm_id: 'vm-bbbbbbbb', state: 'running', sessions: 2 },
        ],
      },
      { device_id: 'd2', kind: 'org', online: false, capacity: null, assigned_via: 'workgroup', vms: [] },
    ]);
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
    expect((await loadWorkgroupDevices(t.context, t.wg)).placement).toBeNull();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: 'x' }));
    expect(await refusal(loadWorkgroupDevices(t.context, t.wg))).toBe('unavailable');
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => null);
    expect(await refusal(loadWorkgroupDevices(t.context, t.wg))).toBe('unavailable');
  });

  it('preselects only a listed device and VM and compares placements', () => {
    const devices = [
      {
        device_id: 'd1',
        kind: 'org',
        online: true,
        capacity: null,
        assigned_via: 'workgroup' as const,
        vms: [{ vm_id: 'vm-aaaaaaaa', state: 'running', sessions: 1 }],
      },
    ];
    expect(validChoice(devices, null)).toBeNull();
    expect(validChoice(devices, { device_id: 'gone', vm_id: null })).toBeNull();
    expect(validChoice(devices, { device_id: 'd1', vm_id: 'vm-aaaaaaaa' })).toEqual({
      device_id: 'd1',
      vm_id: 'vm-aaaaaaaa',
    });
    expect(validChoice(devices, { device_id: 'd1', vm_id: 'vm-gone0000' })).toEqual({ device_id: 'd1', vm_id: null });
    expect(samePlacement(null, null)).toBe(true);
    expect(samePlacement({ device_id: 'd1', vm_id: null }, null)).toBe(false);
    expect(samePlacement({ device_id: 'd1', vm_id: null }, { device_id: 'd1', vm_id: 'vm-aaaaaaaa' })).toBe(false);
    expect(samePlacement({ device_id: 'd1', vm_id: 'vm-a' }, { device_id: 'd1', vm_id: 'vm-a' })).toBe(true);
    expect(placementOf({ device_id: 'd1' })).toEqual({ device_id: 'd1', vm_id: null });
    expect(placementOf({ device_id: 1 })).toBeNull();
    expect(placementBody(null)).toEqual({ device_id: null, vm_id: null });
    expect(placementBody({ device_id: 'd1', vm_id: 'vm-a' })).toEqual({ device_id: 'd1', vm_id: 'vm-a' });
    expect([...PLACEMENT_REASONS].sort()).toEqual(['placement_full', 'placement_offline']);
  });
});

describe('the placement routes', () => {
  it('stores the workgroup default and refuses malformed answers', async () => {
    const t = await taskWorld();
    const route = `PUT /v1/workgroups/${t.wg}/placement`;
    t.client.on(route, (body) => ({
      placement: (body as { device_id: string | null }).device_id === null ? null : body,
    }));
    expect(await setWorkgroupPlacement(t.context, t.wg, { device_id: 'd1', vm_id: null })).toEqual({
      device_id: 'd1',
      vm_id: null,
    });
    expect(t.client.of(route)[0]!.body).toEqual({ device_id: 'd1', vm_id: null });
    expect(await setWorkgroupPlacement(t.context, t.wg, null)).toBeNull();
    t.client.on(route, () => ({}));
    expect(await refusal(setWorkgroupPlacement(t.context, t.wg, null))).toBe('unavailable');
    t.client.on(route, () => null);
    expect(await refusal(setWorkgroupPlacement(t.context, t.wg, null))).toBe('unavailable');
  });

  it('changes the choice of a task and stores the answered row', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { state: 'scheduling', state_reason: 'placement_offline' });
    const route = `PUT /v1/tasks/${row.task_id}/placement`;
    t.client.on(route, () => ({ task: { ...row, placement: null } }));
    expect(await releaseTaskPlacement(t.context, row.task_id)).toBe(true);
    expect(await applyAutomatic(t.context, row.task_id, null, true)).toBe(true);
    expect(await applyAutomatic(t.context, row.task_id, null, false)).toBe(false);
    expect(await applyAutomatic(t.context, row.task_id, { device_id: row.task_id, vm_id: null }, true)).toBe(false);
    const changed = await setTaskPlacement(t.context, row.task_id, null);
    expect(changed.placement).toBeNull();
    expect(t.store.getState().tasks.tasks[row.task_id]!.placement).toBeNull();
    expect(t.client.of(route)[2]!.body).toEqual({ device_id: null, vm_id: null });
    t.client.on(route, () => ({ task: {} }));
    expect(await refusal(setTaskPlacement(t.context, row.task_id, null))).toBe('unavailable');
    expect(await releaseTaskPlacement(t.context, row.task_id)).toBe(false);
    t.client.on(route, () => null);
    expect(await refusal(setTaskPlacement(t.context, row.task_id, null))).toBe('unavailable');
  });

  it('reads the chosen device of a task and the VM of a session', async () => {
    const t = await taskWorld();
    const row = taskRow(t) as TaskRow;
    expect(taskPlacement(row)).toBeNull();
    expect(taskPlacement({ ...row, placement: { device_id: 'd1', vm_id: 'vm-a' } })).toEqual({
      device_id: 'd1',
      vm_id: 'vm-a',
    });
    const session = sessionRow(t) as SessionRow;
    expect(sessionVm(session)).toBeNull();
    expect(sessionVm({ ...session, vm_id: 'vm-1a2b3c4d' })).toBe('vm-1a2b3c4d');
  });
});
