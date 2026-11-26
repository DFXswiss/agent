// Job 4 of 06-web.md section 11: pending rotations, sent automatically and silently (04-crypto.md section 4.7, ADR-058
// section 3). The
// hub's `rotation_pending` of an org is derived from two facts the browser verifies itself, so the job works from the
// verified logs only (a hub cannot make a browser rotate or unassign anything the logs do not owe):
// 1. platform devices of the org whose certification ended (verified platform log, refreshed first) but that are still
//    assigned: an admin owes `device_unassign_org` (assigned to the organization, ADR-075) and `device_unassign` for
//    every explicit assignment of such a device plus R (built by `buildBatch`);
// 2. scopes a member could read before leaving by itself or an admin before demoting itself (`OrgState.pending`):
//    one `rotate` per scope the user may rotate (org: admin; workgroup: admin or writer), with the wraps of the new
//    epochs for every reader;
// 3. workgroups the user reads only as admin whose current key nobody left can deliver (`keyHolderLeft` false: no
//    explicit member, no assigned device, the author of the current epoch gone; ADR-058 section 2): an admin rotates
//    them so that new content is readable for every reader again (older epochs stay deliverable by whoever holds
//    them, so the rotation loses nothing); archived workgroups are left alone;
// 4. workgroups released to DFX support (ADR-096 section 3) that the hub lists in the org's `rotation_pending` after a
//    person left support (`GET /v1/orgs/{org}`): rotated by an admin or a user with the release right, but only while
//    the verified platform log shows a former support person (`supportAuthors` beyond `supportRoster`) and the
//    verified org log an active release of a writable workgroup – the hub alone cannot make a browser rotate. The new
//    epoch reaches the remaining support persons by catch-up.
// Each batch goes through `submitBatch` (rebuilt after `seq_conflict`); a batch another browser sent first leaves
// nothing to do. Nothing is shown (key handling stays out of sight, ADR-058 section 4).
import type { LogFields } from '../log/entries';
import type { PlatformState } from '../log/platform';
import { unassignAll } from '../log/rotation';
import { ORG, WG, WRITE } from '../log/state';
import type { KeyRing } from '../log/catchup';
import type { OrgState } from '../log/state';
import { keyRing, type OrgContext, type Planner, submitBatch } from '../org/batch';
import { keyHolderLeft } from '../org/unlock';
import { isRecord } from '../registry';
import { authorsOf, rosterOf } from '../support/names';
import { orgEntry } from '../store/slices/orgs';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';
import { refreshPlatform, verifyOrgLog } from './log-verify';

/** At most this many batches per run (the unassignment of decertified devices, then the owed rotations). */
export const MAX_BATCHES = 2;

/** Released workgroups `me` may rotate after a person left support (point 4; empty without a former person). */
export function supportRotatable(state: OrgState, platform: PlatformState | null, me: string): string[] {
  const roster = rosterOf(platform);
  const former = [...authorsOf(platform).keys()].some((id) => !roster.has(id));
  if (!former) return [];
  return [...state.support.keys()].filter((wg) => state.writable(wg) && state.canRelease(me, wg)).sort();
}

/** The workgroups of the hub's `rotation_pending` (`GET /v1/orgs/{org}`); an answer without them names none. */
export async function pendingWorkgroups(context: Pick<JobContext, 'client'>, org: string): Promise<Set<string>> {
  const body = await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}`);
  const pending = isRecord(body) && Array.isArray(body.rotation_pending) ? body.rotation_pending : [];
  return new Set(
    pending
      .filter(isRecord)
      .flatMap((item) => (item.scope === WG && typeof item.scope_id === 'string' ? [item.scope_id] : [])),
  );
}

/** The next batch the user owes, or null; `supportPending`: released workgroups the hub lists (point 4). */
export function owedPlanner(
  platform: PlatformState | null,
  held: KeyRing = () => null,
  supportPending: ReadonlySet<string> = new Set(),
): Planner {
  return (log, me) => {
    const state = log.state;
    if (platform !== null && state.isAdmin(me)) {
      const entries = platform.excluded(state).flatMap((device) => unassignAll(state, device));
      if (entries.length > 0) return { entries };
    }
    const scopes = state
      .pendingScopes()
      .filter(([scope, id]) =>
        scope === ORG
          ? state.isAdmin(me)
          : state.workgroups.has(id) && (state.isAdmin(me) || state.wgAccess(me, id) === WRITE),
      );
    const released = supportRotatable(state, platform, me)
      .filter((wg) => supportPending.has(wg) && !scopes.some(([scope, id]) => scope === WG && id === wg))
      .map((wg): [string, string] => [WG, wg]);
    const entries = [...scopes, ...released].map(([scope, id]): LogFields => ({
      type: 'rotate',
      scope: scope as 'org' | 'wg',
      scope_id: id,
      epoch: state.epoch(scope, id) + 1,
    }));
    if (entries.length > 0) return { entries };
    const orphans = state.isAdmin(me)
      ? [...state.workgroups.keys()].filter(
          (wg) => state.writable(wg) && held(WG, wg, state.epoch(WG, wg)) === null && !keyHolderLeft(state, me, wg),
        )
      : [];
    return orphans.length === 0
      ? null
      : {
          entries: orphans.map((wg): LogFields => ({
            type: 'rotate',
            scope: WG,
            scope_id: wg,
            epoch: state.epoch(WG, wg) + 1,
          })),
        };
  };
}

/** One run for `org`: up to `MAX_BATCHES` owed batches; returns how many were sent. */
export async function rotatePending(context: JobContext, run: JobRun): Promise<number> {
  const { store } = context;
  if (store.getState().me.identity === null || orgEntry(store.getState().orgs, run.org).log === null) return 0;
  const platform = (await refreshPlatform(context, run.generation))?.state ?? null;
  const orgContext: OrgContext = {
    store,
    client: context.client,
    now: context.now,
    refreshLog: (org) => verifyOrgLog(context, org, run.generation),
  };
  const verified = orgEntry(store.getState().orgs, run.org).log!;
  const user = store.getState().me.userId!;
  // the hub's list is read only when the verified logs could owe such a rotation (point 4)
  const rotatable = supportRotatable(verified.state, platform, user);
  const pending = rotatable.length === 0 ? new Set<string>() : await pendingWorkgroups(context, run.org);
  const owed = owedPlanner(platform, keyRing(store, run.org), pending);
  const planner: Planner = (log, me) => (current(context, run) ? owed(log, me) : null);
  let sent = 0;
  while (sent < MAX_BATCHES) {
    const { built } = await submitBatch(orgContext, run.org, planner);
    if (built === null) break;
    sent += 1;
    // the hub's list was read before: what this batch rotated is owed no more
    for (const entry of built.body.entries) pending.delete(entry.body.scope_id as string);
  }
  return sent;
}

export const job: JobRegistration = {
  name: 'rotation',
  order: 40,
  triggers: ['login', 'log', 'interval'],
  run: async (context: JobContext, run: JobRun) => {
    await rotatePending(context, run);
  },
};
