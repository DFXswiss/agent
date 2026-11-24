// Job 4 of 06-web.md section 11: pending rotations, sent automatically and silently (04-crypto.md section 4.7, ADR-058
// section 3). The
// hub's `rotation_pending` of an org is derived from two facts the browser verifies itself, so the job works from the
// verified logs only (a hub cannot make a browser rotate or unassign anything the logs do not owe):
// 1. platform devices of the org whose certification ended (verified platform log, refreshed first) but that are still
//    assigned: an admin owes `device_unassign_org` (assigned to the organization, ADR-060) and `device_unassign` for
//    every explicit assignment of such a device plus R (built by `buildBatch`);
// 2. scopes a member could read before leaving by itself or an admin before demoting itself (`OrgState.pending`):
//    one `rotate` per scope the user may rotate (org: admin; workgroup: admin or writer), with the wraps of the new
//    epochs for every reader;
// 3. workgroups the user reads only as admin whose current key nobody left can deliver (`keyHolderLeft` false: no
//    explicit member, no assigned device, the author of the current epoch gone; ADR-058 section 2): an admin rotates
//    them so that new content is readable for every reader again (older epochs stay deliverable by whoever holds
//    them, so the rotation loses nothing); archived workgroups are left alone.
// Each batch goes through `submitBatch` (rebuilt after `seq_conflict`); a batch another browser sent first leaves
// nothing to do. Nothing is shown (key handling stays out of sight, ADR-058 section 4).
import type { LogFields } from '../log/entries';
import type { PlatformState } from '../log/platform';
import { unassignAll } from '../log/rotation';
import { ORG, WG, WRITE } from '../log/state';
import type { KeyRing } from '../log/catchup';
import { keyRing, type OrgContext, type Planner, submitBatch } from '../org/batch';
import { keyHolderLeft } from '../org/unlock';
import { orgEntry } from '../store/slices/orgs';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';
import { refreshPlatform, verifyOrgLog } from './log-verify';

/** At most this many batches per run (the unassignment of decertified devices, then the owed rotations). */
export const MAX_BATCHES = 2;

/** The next batch the user owes, or null. */
export function owedPlanner(platform: PlatformState | null, held: KeyRing = () => null): Planner {
  return (log, me) => {
    const state = log.state;
    if (platform !== null && state.isAdmin(me)) {
      const entries = platform.excluded(state).flatMap((device) => unassignAll(state, device));
      if (entries.length > 0) return { entries };
    }
    const entries = state
      .pendingScopes()
      .filter(([scope, id]) =>
        scope === ORG
          ? state.isAdmin(me)
          : state.workgroups.has(id) && (state.isAdmin(me) || state.wgAccess(me, id) === WRITE),
      )
      .map(([scope, id]): LogFields => ({
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
  const owed = owedPlanner(platform, keyRing(store, run.org));
  const planner: Planner = (log, me) => (current(context, run) ? owed(log, me) : null);
  let sent = 0;
  while (sent < MAX_BATCHES) {
    const { built } = await submitBatch(orgContext, run.org, planner);
    if (built === null) break;
    sent += 1;
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
