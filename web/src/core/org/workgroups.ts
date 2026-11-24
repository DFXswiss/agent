// Workgroups of an org (06-web.md section 7.2; 04-crypto.md section 4.7; 03-api.md sections 4.3, 4.4): the rows of the
// list from the verified log (names from `wg.profile`), and the changes.
//
// - Create: one batch `wg_create` (the creating admin gets write), `grant` for the chosen members, `device_assign` for
//   the chosen devices of the org, the wraps of the new `WK_1` for every reader (built by `buildBatch`) and the
//   `wg.profile` event sealed with the current `OK` at `lh` = the batch's last entry (the hub checks events against
//   the log after the batch; the workgroup must exist at `lh`). Every other admin reads it by inheritance (ADR-058),
//   so `WK_1` is wrapped for every admin too.
// - Rename: a new `wg.profile` event (`POST /v1/events`), read into the store at once.
// - Archive: `wg_archive` (never the default workgroup).
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import type { Identity } from '../crypto/keys';
import type { KeyRing } from '../log/catchup';
import type { LogFields } from '../log/entries';
import { excludedFor, type PlatformState } from '../log/platform';
import { DEVICE, ORG, type OrgState } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { Directory } from '../tasks/directory';
import type { TasksContext } from '../tasks/pull';
import { actorOf, keyRing, type OrgContext, type Planner, refuse } from './batch';
import { type Access, workgroupOrder } from './members';

/** Workgroup names are 1–80 characters after trimming (like display names). */
export const NAME_MAX = 80;

/** The trimmed name, or null if it is empty or longer than `NAME_MAX` code points. */
export function workgroupName(input: string): string | null {
  const name = input.trim();
  const length = [...name].length;
  return length >= 1 && length <= NAME_MAX ? name : null;
}

/** One row of the workgroup list. */
export interface WorkgroupRow {
  readonly id: string;
  /** From the newest verified `wg.profile`, null if none was read. */
  readonly name: string | null;
  readonly isDefault: boolean;
  readonly archived: boolean;
  readonly myAccess: Access | null;
  /** Active devices assigned to it, sorted. */
  readonly devices: readonly string[];
}

/** Every workgroup of the org: the default one first, then active ones, then archived, each by ID. */
export function workgroupRows(state: OrgState, me: string, directory: Directory): WorkgroupRow[] {
  return workgroupOrder(state)
    .map((id) => ({
      id,
      name: directory.workgroups.get(id) ?? null,
      isDefault: id === state.defaultWg,
      archived: !state.writable(id),
      myAccess: state.wgAccess(me, id) as Access | null,
      devices: [...state.principals.keys()].filter((p) => state.assigned(p, id)).sort(),
    }))
    .sort((a, b) => Number(a.archived) - Number(b.archived));
}

/**
 * Devices an admin can assign to a new workgroup: active devices of the org that are not decertified; platform
 * devices only while the org has platform access (ADR-026); not the devices of the whole organization (ADR-075: they
 * are assigned to the new workgroup already and receive its `WK_1` from the batch). With their `platform_seq` (null
 * for org devices).
 */
export function assignableDevices(
  state: OrgState,
  platform: PlatformState | null,
): { id: string; platformSeq: number | null }[] {
  const excluded = new Set(excludedFor(platform, state));
  const access = platform?.platformAccess(state.org, state.private) ?? false;
  return [...state.principals.values()]
    .filter((p) => p.kind === DEVICE && p.removedSeq === null && !excluded.has(p.id) && !state.orgAssigned(p.id))
    .filter((p) => p.platformSeq === null || access)
    .map((p) => ({ id: p.id, platformSeq: p.platformSeq }))
    .sort((a, b) => Number(a.id > b.id) - Number(a.id < b.id));
}

/** What the admin chooses for a new workgroup. */
export interface NewWorkgroup {
  /** The new workgroup ID (drawn once by the caller so that a retry creates the same workgroup). */
  readonly wg: string;
  readonly name: string;
  /** Members to grant access (the creating admin always has write). */
  readonly grants: Readonly<Record<string, Access>>;
  readonly devices: readonly string[];
}

/** The `wg.profile` event of `wg` sealed with the current `OK` of `state` at `lh`. */
async function profileEvent(
  state: OrgState,
  key: KeyRing,
  author: { id: string; identity: Identity },
  wg: string,
  name: string,
  lh: number,
  now: number,
): Promise<unknown> {
  const org = state.org;
  const epoch = state.epoch(ORG, org);
  const orgKey = key(ORG, org, epoch) ?? refuse('epoch_stale');
  const header = newHeader({
    id: uuid7(now),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: author.id,
    kind: 'wg.profile',
    ts: formatTime(now),
    lh,
    wg,
  });
  return sealEvent(orgKey, header, bodyJson({ name }), author.identity.sig.privateKey);
}

/** The batch of a new workgroup; null once the workgroup exists (a retry after the hub took it). */
export function createWorkgroupPlan(input: NewWorkgroup, now: () => number): Planner {
  return (log, me) => {
    const state = log.state;
    if (!state.isAdmin(me)) refuse('forbidden');
    if (state.workgroups.has(input.wg)) return null;
    const name = workgroupName(input.name) ?? refuse('params_invalid');
    const members = Object.entries(input.grants)
      .filter(([user]) => user !== me)
      .sort(([a], [b]) => Number(a > b) - Number(a < b));
    const devices = [...input.devices].sort();
    const entries: LogFields[] = [
      { type: 'wg_create', wg: input.wg, epoch: 1 },
      ...members.map(([subject, access]): LogFields => ({ type: 'grant', subject, wg: input.wg, access })),
      ...devices.map((device): LogFields => {
        const platformSeq = state.member(device)?.platformSeq ?? null;
        return platformSeq === null
          ? { type: 'device_assign', device, wg: input.wg }
          : { type: 'device_assign', device, wg: input.wg, platform_seq: platformSeq };
      }),
    ];
    return {
      entries,
      events: async (built, keys, actor) => [
        await profileEvent(
          built.log.state,
          keys,
          { id: actor.userId, identity: actor.identity },
          input.wg,
          name,
          built.log.head.seq,
          now(),
        ),
      ],
    };
  };
}

/** `wg_archive`; null once archived; never the default workgroup. */
export function archivePlan(wg: string): Planner {
  return (log, me) => {
    const state = log.state;
    if (!state.isAdmin(me)) refuse('forbidden');
    if (wg === state.defaultWg || !state.workgroups.has(wg)) refuse('forbidden');
    if (!state.writable(wg)) return null;
    return { entries: [{ type: 'wg_archive', wg }] };
  };
}

/** Renames a workgroup: a new `wg.profile` event, read into the store at once. */
export async function renameWorkgroup(
  context: OrgContext & Pick<TasksContext, 'ingest'>,
  org: string,
  wg: string,
  input: string,
): Promise<void> {
  const name = workgroupName(input) ?? refuse('params_invalid');
  const actor = actorOf(context.store, org);
  const state = actor.log.state;
  if (!state.isAdmin(actor.userId)) refuse('forbidden');
  if (!state.workgroups.has(wg)) refuse('not_found');
  const event = await profileEvent(
    state,
    keyRing(context.store, org),
    { id: actor.userId, identity: actor.identity },
    wg,
    name,
    actor.log.head.seq,
    context.now(),
  );
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== org || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(org, org, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
}
