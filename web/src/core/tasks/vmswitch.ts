// The switch of a workgroup VM to the newest golden of its device (ADR-093 sections 3 and 5; 03-api.md section 4.5).
//
// - `upgradeOf`: the `upgrade` of a VM in `GET /v1/workgroups/{wg}` (contract `vm_upgrade`): the golden it moves to,
//   `waiting` or `switching`, the holds (who or what keeps it on the old golden, since when) and the paused sessions
//   that move with it. Malformed: null (nothing shown). A hold reason this client does not know is kept as `unknown`,
//   so a held VM never reads as "moves by itself". `checkpoint_impossible` (HOT-24) names a session that can never be
//   saved on this VM; its `checkpoint_at` is the save it would continue from after a switch by hand (null: fresh).
// - `withoutState`: those sessions - the ones a switch by hand continues without the state of the old VM.
// - `outdatedVms`: the VMs of the workgroup's devices that report an upgrade, in device and VM order.
// - `switchStatus`: `switching`, `moves` (waiting without holds: it moves at the next pass) or `held`.
// - `requestVmSwitch`: `POST /v1/workgroups/{wg}/vms/{vm}/switch` with `{}` (members with administration rights; the
//   hub answers `403 forbidden` otherwise): the preview as stored and whether the switch is requested.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { TasksContext } from './pull';
import type { WorkgroupDevice } from './placement';

export const HOLD_REASONS = [
  'session_active',
  'subagent',
  'checkpoint_due',
  'checkpoint_missing',
  'workspace_incomplete',
  'checkpoint_impossible',
  'capacity',
  'switch_failed',
] as const;

export type HoldReason = (typeof HOLD_REASONS)[number] | 'unknown';

export interface VmUpgradeHold {
  readonly reason: HoldReason;
  readonly session_id: string | null;
  readonly since: string;
  readonly checkpoint_at: string | null;
}

export interface VmUpgrade {
  readonly golden_version: string;
  readonly golden_built_at: string | null;
  readonly state: 'waiting' | 'switching';
  readonly holds: readonly VmUpgradeHold[];
  readonly sessions: readonly string[];
}

const KNOWN: ReadonlySet<string> = new Set(HOLD_REASONS);

const holdOf = (value: unknown): VmUpgradeHold | null => {
  if (!isRecord(value) || typeof value.reason !== 'string' || typeof value.since !== 'string') return null;
  return {
    reason: KNOWN.has(value.reason) ? (value.reason as HoldReason) : 'unknown',
    session_id: typeof value.session_id === 'string' ? value.session_id : null,
    since: value.since,
    checkpoint_at: typeof value.checkpoint_at === 'string' ? value.checkpoint_at : null,
  };
};

/** The switch status of a VM (module comment), null when absent or malformed. */
export function upgradeOf(value: unknown): VmUpgrade | null {
  if (!isRecord(value) || typeof value.golden_version !== 'string') return null;
  if (value.state !== 'waiting' && value.state !== 'switching') return null;
  const holds = Array.isArray(value.holds) ? value.holds.map(holdOf).filter((hold) => hold !== null) : [];
  const sessions = Array.isArray(value.sessions)
    ? value.sessions.filter((item): item is string => typeof item === 'string')
    : [];
  return {
    golden_version: value.golden_version,
    golden_built_at: typeof value.golden_built_at === 'string' ? value.golden_built_at : null,
    state: value.state,
    holds,
    sessions,
  };
}

/** A session a switch by hand continues without the state of the old VM, with the save it continues from. */
export interface WithoutState {
  readonly session_id: string;
  readonly checkpoint_at: string | null;
}

export function withoutState(upgrade: VmUpgrade): WithoutState[] {
  return upgrade.holds.flatMap((hold) =>
    hold.reason === 'checkpoint_impossible' && hold.session_id !== null
      ? [{ session_id: hold.session_id, checkpoint_at: hold.checkpoint_at }]
      : [],
  );
}

/** An outdated VM of the workgroup with its device. */
export interface OutdatedVm {
  readonly device_id: string;
  readonly vm_id: string;
  readonly upgrade: VmUpgrade;
}

export function outdatedVms(devices: readonly WorkgroupDevice[]): OutdatedVm[] {
  return devices.flatMap((device) =>
    device.vms.flatMap((vm) =>
      vm.upgrade === undefined ? [] : [{ device_id: device.device_id, vm_id: vm.vm_id, upgrade: vm.upgrade }],
    ),
  );
}

export type SwitchStatus = 'switching' | 'moves' | 'held';

export function switchStatus(upgrade: VmUpgrade): SwitchStatus {
  if (upgrade.state === 'switching') return 'switching';
  return upgrade.holds.length === 0 ? 'moves' : 'held';
}

/** A short form of a session ID for display (its last 8 characters). */
export const shortId = (id: string): string => id.slice(-8);

/** What the hub answered to a switch request. */
export interface SwitchAnswer {
  readonly requested: boolean;
  readonly requested_at: string | null;
  readonly upgrade: VmUpgrade | null;
}

/** `POST /v1/workgroups/{wg}/vms/{vm}/switch` (module comment). */
export async function requestVmSwitch(
  context: Pick<TasksContext, 'client'>,
  wg: string,
  vm: string,
): Promise<SwitchAnswer> {
  const answer = await context.client.request(
    'POST',
    `/v1/workgroups/${encodeURIComponent(wg)}/vms/${encodeURIComponent(vm)}/switch`,
    { body: {} },
  );
  if (!isRecord(answer) || typeof answer.requested !== 'boolean') throw new HubError('unavailable', 200);
  return {
    requested: answer.requested,
    requested_at: typeof answer.requested_at === 'string' ? answer.requested_at : null,
    upgrade: upgradeOf(answer.upgrade),
  };
}
