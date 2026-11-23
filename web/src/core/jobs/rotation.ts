// Job 4 of 06-web.md section 11: pending rotations, sent automatically with a toast (04-crypto.md section 4.7). The
// hub's `rotation_pending` of an org is derived from two facts the browser verifies itself, so the job works from the
// verified logs only (a hub cannot make a browser rotate or unassign anything the logs do not owe):
// 1. platform devices of the org whose certification ended (verified platform log, refreshed first) but that are still
//    assigned: an admin owes `device_unassign` for every workgroup of such a device plus R (built by `buildBatch`);
// 2. scopes a member could read before leaving by itself (`OrgState.pending`): one `rotate` per scope the user may
//    rotate (org: admin; workgroup: admin or writer), with the wraps of the new epochs for every reader.
// Each batch goes through `submitBatch` (rebuilt after `seq_conflict`); a batch another browser sent first leaves
// nothing to do. Every sent batch leaves a notice in the `rotation` slice (the toast).
import type { LogFields } from '../log/entries';
import type { PlatformState } from '../log/platform';
import { ORG, pairKey, WRITE } from '../log/state';
import { type OrgContext, type Planner, submitBatch } from '../org/batch';
import { noteRotation } from '../store/slices/rotation';
import { orgEntry } from '../store/slices/orgs';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';
import { refreshPlatform, verifyOrgLog } from './log-verify';

/** At most this many batches per run (the unassignment of decertified devices, then the owed rotations). */
export const MAX_BATCHES = 2;

/** The next batch the user owes, or null. */
export function owedPlanner(platform: PlatformState | null): Planner {
  return (log, me) => {
    const state = log.state;
    if (platform !== null && state.isAdmin(me)) {
      const entries: LogFields[] = platform
        .excluded(state)
        .flatMap((device) => state.deviceWgs(device).map((wg): LogFields => ({ type: 'device_unassign', device, wg })));
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
    return entries.length === 0 ? null : { entries };
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
  const owed = owedPlanner(platform);
  const planner: Planner = (log, me) => (current(context, run) ? owed(log, me) : null);
  let sent = 0;
  while (sent < MAX_BATCHES) {
    const { built } = await submitBatch(orgContext, run.org, planner);
    if (built === null) break;
    sent += 1;
    const bodies = built.body.entries.map((entry) => entry.body);
    noteRotation(store, {
      org: run.org,
      scopes: new Set(
        bodies.filter((b) => b.type === 'rotate').map((b) => pairKey(String(b.scope), String(b.scope_id))),
      ).size,
      devices: new Set(bodies.filter((b) => b.type === 'device_unassign').map((b) => String(b.device))).size,
      at: context.now(),
    });
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
