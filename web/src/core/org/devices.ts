// Devices of an org (06-web.md section 7.3; 03-api.md sections 4.5, 4.6.2; 04-crypto.md sections 5.8-5.10, 6.2;
// ADR-007, ADR-023, ADR-026).
//
// The list is the verified log's: every active device member of the org (newest first) with its kind (platform
// devices carry the `platform_seq` of their certification), its assigned workgroups and – for platform devices – whether
// the verified platform log still certifies it. The hub's device list (`GET /v1/orgs/{org}/devices`) only adds what the
// hub may know in plaintext: presence, last seen, agent version and capacity (`capacity_low`, 03-api.md section
// 4.6.2); a device the hub lists but the log does not know is not shown. Labels come from the newest verified
// `device.profile` event of each device (org scope, `ref` = device, written by an admin).
//
// Changes (admin only: `device_assign`, `device_unassign` and `remove` are admin entries of the log, 04-crypto.md
// section 4.7; in a private org the single member is the admin):
// - assign a device to a workgroup: one batch `device_assign` (platform devices with `platform_seq`, only with platform
//   access, ADR-026) + the wraps of the current `WK` and `OK` the user holds for the device (built by `buildBatch`,
//   ADR-007; the transition rule of ADR-059 withholds a `WK` the user reads only as admin, catch-up delivers it), then the
//   wrap of the user's own `CK` (`wrapCredentialKey`, best effort like the first-task flow);
// - platform candidates (`GET /v1/workgroups/{wg}/device-candidates`) are offered only when the verified platform log
//   certifies them with the listed keys (compiled root key);
// - unassign (`device_unassign`) and unpair (`remove` of the device): `submitBatch` adds the rotation of R with the new
//   keys for every remaining recipient (04-crypto.md section 5.10);
// - rename: a new `device.profile` event sealed with the current `OK`.
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import type { LogFields } from '../log/entries';
import type { PlatformState } from '../log/platform';
import { DEVICE, ORG, type OrgState } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { memoize } from '../store/selectors';
import type { ScopeEvents } from '../store/slices/events';
import type { StoreState } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import { certified, wrapCredentialKey } from '../tasks/send';
import {
  actorOf,
  keyRing,
  type Planner,
  refuse,
  submitBatch,
  type Submitted,
  type SubmitProgress,
  verifiedPlatform,
} from './batch';
import { workgroupOrder } from './members';

export const DEVICE_PROFILE = 'device.profile';
/** Device labels are 1–100 characters after trimming (`contracts/events/objects.json`, `body_device_profile`). */
export const LABEL_MAX = 100;
/** The enrollment command shown with a new org device invitation (06-web.md section 7.3). */
export const enrollCommand = (origin: string, code: string): string =>
  `agent-runner enroll --hub ${origin} --code ${code}`;

/** The trimmed label, or null if it is empty, longer than `LABEL_MAX` code points or not a single line. */
export function deviceLabel(input: string): string | null {
  const label = input.trim();
  const length = [...label].length;
  return length >= 1 && length <= LABEL_MAX && !/[\r\n]/.test(label) ? label : null;
}

// --- the hub's view ---

export interface Capacity {
  readonly vm_memory_budget_mib: number;
  readonly vm_memory_used_mib: number;
  readonly vm_slots_total: number;
  readonly vm_slots_free: number;
  readonly pool_ready: number;
}

const CAPACITY_FIELDS = ['vm_memory_budget_mib', 'vm_memory_used_mib', 'vm_slots_total', 'vm_slots_free', 'pool_ready'];

const count = (value: unknown): boolean => Number.isSafeInteger(value) && (value as number) >= 0;

export const isCapacity = (value: unknown): value is Capacity =>
  isRecord(value) && CAPACITY_FIELDS.every((field) => count(value[field]));

/** One row of `GET /v1/orgs/{org}/devices`. */
export interface HubDevice {
  readonly device_id: string;
  readonly kind: 'org' | 'platform';
  readonly state: string;
  readonly online: boolean;
  readonly last_seen_at: string | null;
  readonly agent_version: string;
  readonly capacity: Capacity;
  readonly capacity_low: boolean;
  readonly assigned_wgs: readonly string[];
}

const isHubDevice = (value: unknown): value is HubDevice =>
  isRecord(value) &&
  typeof value.device_id === 'string' &&
  (value.kind === 'org' || value.kind === 'platform') &&
  typeof value.state === 'string' &&
  typeof value.online === 'boolean' &&
  (value.last_seen_at === null || typeof value.last_seen_at === 'string') &&
  typeof value.agent_version === 'string' &&
  isCapacity(value.capacity) &&
  typeof value.capacity_low === 'boolean' &&
  Array.isArray(value.assigned_wgs) &&
  value.assigned_wgs.every((wg) => typeof wg === 'string');

/** `GET /v1/orgs/{org}/devices` (`unavailable` for a malformed answer). */
export async function loadOrgDevices(context: Pick<TasksContext, 'client'>, org: string): Promise<HubDevice[]> {
  const body = await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/devices`);
  if (!isRecord(body) || !Array.isArray(body.devices) || !body.devices.every(isHubDevice)) {
    throw new HubError('unavailable', 200);
  }
  return body.devices;
}

/** Used and total VM slots and the used share (0 for a device without slots). */
export function capacityUse(capacity: Capacity): { readonly used: number; readonly total: number; share: number } {
  const total = capacity.vm_slots_total;
  const used = Math.min(total, Math.max(0, total - capacity.vm_slots_free));
  return { used, total, share: total === 0 ? 0 : used / total };
}

// --- rows ---

/** One row of the device list. */
export interface DeviceRow {
  readonly id: string;
  /** From the newest verified `device.profile`, null if none was read. */
  readonly label: string | null;
  readonly kind: 'org' | 'platform';
  readonly platformSeq: number | null;
  /** A platform device whose certification (the one its keys come from) has ended. */
  readonly decertified: boolean;
  /** Assigned workgroups from the verified log, in the order of `workgroupOrder`. */
  readonly workgroups: readonly string[];
  /** What the hub reports (null while its list does not name the device). */
  readonly hub: HubDevice | null;
}

/** Active devices of the org, newest member first (then by ID), with the hub's presence and capacity. */
export function deviceRows(
  state: OrgState,
  platform: PlatformState | null,
  hub: readonly HubDevice[],
  labels: ReadonlyMap<string, string>,
): DeviceRow[] {
  // display only: without a verified platform log no device is shown as decertified
  const excluded = new Set(platform === null ? [] : platform.excluded(state));
  const byId = new Map(hub.map((row) => [row.device_id, row]));
  const order = workgroupOrder(state);
  return [...state.principals.values()]
    .filter((p) => p.kind === DEVICE && p.removedSeq === null)
    .sort((a, b) => b.joinedSeq - a.joinedSeq || Number(a.id > b.id) - Number(a.id < b.id))
    .map((p) => ({
      id: p.id,
      label: labels.get(p.id) ?? null,
      kind: p.platformSeq === null ? 'org' : 'platform',
      platformSeq: p.platformSeq,
      decertified: excluded.has(p.id),
      workgroups: order.filter((wg) => state.assigned(p.id, wg)),
      hub: byId.get(p.id) ?? null,
    }));
}

const labelsOf = memoize((scope: ScopeEvents): ReadonlyMap<string, string> => {
  const labels = new Map<string, string>();
  const verified = [...scope.entries.values()]
    .filter((entry) => entry.status === 'verified' && entry.header!.kind === DEVICE_PROFILE)
    .sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const entry of verified) {
    const label = isRecord(entry.body) && typeof entry.body.label === 'string' ? entry.body.label : null;
    if (label !== null) labels.set(entry.header!.ref as string, label);
  }
  return labels;
});

const NO_LABELS: ReadonlyMap<string, string> = new Map();

/** Device ID → label from the verified `device.profile` events of the org scope. */
export const selectDeviceLabels =
  (org: string) =>
  (state: StoreState): ReadonlyMap<string, string> => {
    const scope = state.events[org];
    return scope === undefined ? NO_LABELS : labelsOf(scope);
  };

// --- rights ---

/** Device management (add, assign, unassign, unpair, rename) is rendered only for admins (06-web.md section 1.2). */
export function canManageDevices(state: OrgState, me: string): boolean {
  return state.isAdmin(me);
}

/** ADR-026: a private org has platform access; any other org once the verified platform log grants it. */
export function hasPlatformAccess(state: OrgState, platform: PlatformState | null): boolean {
  return state.private || (platform?.platformAccess(state.org, false) ?? false);
}

/**
 * Workgroups a device can be assigned to: every workgroup that is not archived, in the order of `workgroupOrder`. The
 * batch wraps the keys the user holds for the device; the other key holders deliver the rest by catch-up (ADR-059).
 */
export function assignableWorkgroups(state: OrgState): string[] {
  return workgroupOrder(state).filter((wg) => state.writable(wg));
}

// --- platform candidates ---

/** A platform candidate of `GET /v1/workgroups/{wg}/device-candidates`, and whether the platform log certifies it. */
export interface CandidateRow {
  readonly device_id: string;
  readonly platform_seq: number;
  readonly pub: { readonly kem: string; readonly sig: string };
  readonly online: boolean;
  readonly capacity: Capacity | null;
  readonly capacity_low: boolean;
  readonly verified: boolean;
}

const isCandidate = (value: unknown): value is Omit<CandidateRow, 'verified'> =>
  isRecord(value) &&
  typeof value.device_id === 'string' &&
  Number.isSafeInteger(value.platform_seq) &&
  isRecord(value.pub) &&
  typeof value.pub.kem === 'string' &&
  typeof value.pub.sig === 'string';

const UNVERIFIABLE: Omit<CandidateRow, 'device_id'> = {
  platform_seq: -1,
  pub: { kem: '', sig: '' },
  online: false,
  capacity: null,
  capacity_low: false,
  verified: false,
};

function candidateRow(item: Omit<CandidateRow, 'verified'>, platform: PlatformState | null): CandidateRow {
  return {
    device_id: item.device_id,
    platform_seq: item.platform_seq,
    pub: { kem: item.pub.kem, sig: item.pub.sig },
    online: item.online === true,
    capacity: isCapacity(item.capacity) ? item.capacity : null,
    capacity_low: item.capacity_low === true,
    verified: platform !== null && certified(platform, item),
  };
}

/**
 * The platform candidates of a workgroup in the hub's order (online, most free capacity first), each checked against
 * the verified platform log (`verified` false: not certified, decertified, or other keys than certified). Refreshes
 * the platform log once if a candidate is not verified (a certification newer than the stored log).
 */
export async function loadCandidates(context: TasksContext, org: string, wg: string): Promise<CandidateRow[]> {
  const body = await context.client.request('GET', `/v1/workgroups/${encodeURIComponent(wg)}/device-candidates`);
  if (!isRecord(body) || !Array.isArray(body.candidates)) throw new HubError('unavailable', 200);
  const offered: readonly unknown[] = body.candidates;
  // a malformed candidate is listed as an unverifiable row, never dropped (06-web.md section 1.1)
  const check = (platform: PlatformState | null) =>
    offered.map((value, index): CandidateRow => {
      if (!isCandidate(value)) {
        const id = isRecord(value) && typeof value.device_id === 'string' ? value.device_id : `#${index + 1}`;
        return { ...UNVERIFIABLE, device_id: id };
      }
      return candidateRow(value, platform);
    });
  let rows = check(verifiedPlatform(context.store));
  if (rows.some((row) => !row.verified)) {
    await context.refreshLog(org);
    rows = check(verifiedPlatform(context.store));
  }
  return rows;
}

// --- planners ---

function requireAdmin(state: OrgState, me: string): void {
  if (!state.isAdmin(me)) refuse('forbidden');
}

/** `device_assign` of `device` to `wg` (`platformSeq` for a platform device); null once it is assigned. */
export function assignPlan(device: string, wg: string, platformSeq: number | null): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    if (!state.writable(wg)) refuse('forbidden');
    if (state.assigned(device, wg)) return null;
    const found = state.member(device);
    if (platformSeq === null) {
      if (found?.kind !== DEVICE || found.platformSeq !== null) refuse('not_found');
      return { entries: [{ type: 'device_assign', device, wg }] };
    }
    if (found !== null && found.platformSeq !== platformSeq) refuse('log_rights');
    return { entries: [{ type: 'device_assign', device, wg, platform_seq: platformSeq }] };
  };
}

/** `device_unassign` (+ R: the workgroup, and the org once the device has no workgroup left); null once done. */
export function unassignPlan(device: string, wg: string): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    if (!state.assigned(device, wg)) return null;
    return { entries: [{ type: 'device_unassign', device, wg }] };
  };
}

/** Unpair: `remove` of the device (+ R: every workgroup it was assigned to and the org); null once it is gone. */
export function unpairPlan(device: string): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    const found = state.member(device);
    if (found === null) return null;
    if (found.kind !== DEVICE) refuse('forbidden');
    return { entries: [{ type: 'remove', subject: device } satisfies LogFields] };
  };
}

/** What `assignDevice` assigns: an org device of the log, or a verified platform candidate. */
export type AssignTarget =
  | { readonly kind: 'org'; readonly device: string }
  | { readonly kind: 'platform'; readonly device: string; readonly platformSeq: number };

/**
 * Assigns a device to a workgroup (the batch with `WK` and `OK` for the device), then wraps the user's own `CK` for it.
 * Platform devices need platform access (`platform_access_denied`, ADR-026) and a valid certification in the verified
 * platform log with the device's keys.
 */
export async function assignDevice(
  context: TasksContext,
  org: string,
  wg: string,
  target: AssignTarget,
  progress?: (step: SubmitProgress) => void,
): Promise<Submitted> {
  const actor = actorOf(context.store, org);
  const platform = verifiedPlatform(context.store);
  let kem: string;
  let platformSeq: number | null = null;
  if (target.kind === 'platform') {
    if (!hasPlatformAccess(actor.log.state, platform)) refuse('platform_access_denied');
    const cert = platform?.certification(target.platformSeq) ?? null;
    if (cert === null || cert.device !== target.device || !platform!.valid(target.platformSeq)) refuse('log_rights');
    kem = cert.kem;
    platformSeq = target.platformSeq;
  } else {
    kem = actor.log.state.member(target.device)?.kem ?? refuse('not_found');
  }
  const submitted = await submitBatch(context, org, assignPlan(target.device, wg, platformSeq), progress);
  if (submitted.built !== null) {
    await wrapCredentialKey(context, { userId: actor.userId, identity: actor.identity }, { id: target.device, kem });
  }
  return submitted;
}

/** Renames a device: a new `device.profile` event (org scope, `ref` = device), read into the store at once. */
export async function renameDevice(context: TasksContext, org: string, device: string, input: string): Promise<void> {
  const label = deviceLabel(input) ?? refuse('params_invalid');
  const actor = actorOf(context.store, org);
  const state = actor.log.state;
  requireAdmin(state, actor.userId);
  if (state.member(device)?.kind !== DEVICE) refuse('not_found');
  const epoch = state.epoch(ORG, org);
  const orgKey = keyRing(context.store, org)(ORG, org, epoch) ?? refuse('epoch_stale');
  const now = context.now();
  const header = newHeader({
    id: uuid7(now),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: actor.userId,
    kind: DEVICE_PROFILE,
    ts: formatTime(now),
    lh: actor.log.head.seq,
    ref: device,
  });
  const event = await sealEvent(orgKey, header, bodyJson({ label }), actor.identity.sig.privateKey);
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== org || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(org, org, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
}
