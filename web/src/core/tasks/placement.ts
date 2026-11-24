// Choosing device and VM (06-web.md sections 4.1, 4.4, 7.3; 03-api.md sections 4.5, 4.6; ADR-075 sections 6, 7).
//
// - `loadWorkgroupDevices`: `GET /v1/workgroups/{wg}` – the devices assigned to the workgroup (directly or through
//   the organization) with presence, capacity and the workgroup's live VMs, and the workgroup's default `placement`
//   (the composer's preselection). Display data only: the hub decides and checks every choice (`403
//   placement_forbidden` for a device that is no candidate, `409 placement_invalid` for a VM that is no live VM of the
//   workgroup on it); keys are never derived from it.
// - `setWorkgroupPlacement`: `PUT /v1/workgroups/{wg}/placement` (writers; `null` = automatic).
// - `setTaskPlacement`: `PUT /v1/tasks/{task}/placement` – "Choose automatically" of a task that waits for its chosen
//   device (`placement_full`, `placement_offline`); the answered task row enters the `tasks` slice.
// - `applyAutomatic`: after sending, "Automatic" over a workgroup default becomes the created task's choice.
// - `PLACEMENT_REASONS`: the waiting reasons of a task with a chosen device that offer "Choose automatically".
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { isTaskRow, putTasks, type SessionRow, type TaskRow } from '../store/slices/tasks';
import { type Capacity, isCapacity } from '../org/devices';
import type { TasksContext } from './pull';

/** A chosen device and optionally one of its VMs (`null` VM = any VM of the device). */
export interface Placement {
  readonly device_id: string;
  readonly vm_id: string | null;
}

/** A live VM of the workgroup on a device with its occupying sessions (`vm_load`). */
export interface VmLoad {
  readonly vm_id: string;
  readonly state: string;
  readonly sessions: number;
}

/** A device of the workgroup (`device_ref`). */
export interface WorkgroupDevice {
  readonly device_id: string;
  readonly kind: string;
  readonly online: boolean;
  readonly capacity: Capacity | null;
  readonly assigned_via: 'workgroup' | 'org';
  readonly vms: readonly VmLoad[];
}

/** What `GET /v1/workgroups/{wg}` says about the workgroup's devices. */
export interface WorkgroupDevices {
  readonly devices: readonly WorkgroupDevice[];
  readonly placement: Placement | null;
}

const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

/** A well-formed placement, else null. */
export function placementOf(value: unknown): Placement | null {
  if (!isRecord(value) || typeof value.device_id !== 'string') return null;
  const vm = typeof value.vm_id === 'string' ? value.vm_id : null;
  return { device_id: value.device_id, vm_id: vm };
}

const vmOf = (value: unknown): VmLoad | null =>
  isRecord(value) && typeof value.vm_id === 'string' && typeof value.state === 'string' && count(value.sessions)
    ? { vm_id: value.vm_id, state: value.state, sessions: value.sessions }
    : null;

const deviceOf = (value: unknown): WorkgroupDevice | null => {
  if (!isRecord(value) || typeof value.device_id !== 'string') return null;
  const vms = Array.isArray(value.vms) ? value.vms.map(vmOf).filter((vm) => vm !== null) : [];
  return {
    device_id: value.device_id,
    kind: typeof value.kind === 'string' ? value.kind : 'org',
    online: value.online === true,
    capacity: isCapacity(value.capacity) ? value.capacity : null,
    assigned_via: value.assigned_via === 'org' ? 'org' : 'workgroup',
    vms: vms.sort((a, b) => Number(a.vm_id > b.vm_id) - Number(a.vm_id < b.vm_id)),
  };
};

/** `GET /v1/workgroups/{wg}`: the devices (malformed rows dropped) and the default placement. */
export async function loadWorkgroupDevices(
  context: Pick<TasksContext, 'client'>,
  wg: string,
): Promise<WorkgroupDevices> {
  const body = await context.client.request('GET', `/v1/workgroups/${encodeURIComponent(wg)}`);
  if (!isRecord(body) || !Array.isArray(body.devices)) throw new HubError('unavailable', 200);
  const devices = body.devices.map(deviceOf).filter((device) => device !== null);
  return { devices, placement: placementOf(body.placement) };
}

/** The request body of the `PUT …/placement` routes (`null` = automatic). */
export const placementBody = (placement: Placement | null) => ({
  device_id: placement?.device_id ?? null,
  vm_id: placement?.vm_id ?? null,
});

/** `PUT /v1/workgroups/{wg}/placement`; returns the stored default. */
export async function setWorkgroupPlacement(
  context: Pick<TasksContext, 'client'>,
  wg: string,
  placement: Placement | null,
): Promise<Placement | null> {
  const answer = await context.client.request('PUT', `/v1/workgroups/${encodeURIComponent(wg)}/placement`, {
    body: placementBody(placement),
  });
  if (!isRecord(answer) || !Object.hasOwn(answer, 'placement')) throw new HubError('unavailable', 200);
  return placementOf(answer.placement);
}

/** `PUT /v1/tasks/{task}/placement`; the answered task row enters the store. */
export async function setTaskPlacement(
  context: Pick<TasksContext, 'client' | 'store'>,
  task: string,
  placement: Placement | null,
): Promise<TaskRow> {
  const answer = await context.client.request('PUT', `/v1/tasks/${encodeURIComponent(task)}/placement`, {
    body: placementBody(placement),
  });
  if (!isRecord(answer) || !isTaskRow(answer.task)) throw new HubError('unavailable', 200);
  putTasks(context.store, [answer.task]);
  return answer.task;
}

/** "Automatic" for a just created task of a workgroup with a default (best effort: false when the hub refused). */
export async function releaseTaskPlacement(
  context: Pick<TasksContext, 'client' | 'store'>,
  task: string,
): Promise<boolean> {
  try {
    await setTaskPlacement(context, task, null);
    return true;
  } catch {
    return false;
  }
}

/** After sending: "Automatic" (`choice` null) over a workgroup default is applied to the created task, since a request
 * without `placement` takes the default; any other case needs nothing (false). */
export async function applyAutomatic(
  context: Pick<TasksContext, 'client' | 'store'>,
  task: string,
  choice: Placement | null,
  hasDefault: boolean,
): Promise<boolean> {
  if (choice !== null || !hasDefault) return false;
  return releaseTaskPlacement(context, task);
}

/** The chosen device and VM of a task row (absent or malformed: null). */
export const taskPlacement = (task: TaskRow): Placement | null => placementOf(task.placement);

/** The VM of a session row (null before `vm_ready` or from an older hub). */
export const sessionVm = (row: SessionRow): string | null => (typeof row.vm_id === 'string' ? row.vm_id : null);

/**
 * The composer's choice from the workgroup's devices: the requested placement if its device (and VM) is still listed,
 * else automatic (a default whose device is no longer a candidate is ignored, ADR-075 section 6).
 */
export function validChoice(devices: readonly WorkgroupDevice[], wanted: Placement | null): Placement | null {
  if (wanted === null) return null;
  const device = devices.find((item) => item.device_id === wanted.device_id);
  if (device === undefined) return null;
  const vm = wanted.vm_id !== null && device.vms.some((item) => item.vm_id === wanted.vm_id) ? wanted.vm_id : null;
  return { device_id: device.device_id, vm_id: vm };
}

/** Both placements name the same device and VM (or both are automatic). */
export const samePlacement = (a: Placement | null, b: Placement | null): boolean =>
  a?.device_id === b?.device_id && (a?.vm_id ?? null) === (b?.vm_id ?? null);

/** The reasons that offer "Choose automatically" (a chosen device is full or offline). */
export const PLACEMENT_REASONS: ReadonlySet<string> = new Set(['placement_full', 'placement_offline']);
