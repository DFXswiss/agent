// The "being unlocked" state of a workgroup (ADR-059 section 2, 06-web.md section 7.2): the signed-in user may read the
// workgroup but holds no key of its current epoch yet (e.g. an admin that reads it by inheritance until a key holder
// delivers the key by catch-up). The views then show one calm line instead of errors or locked entries:
// - `unlocking`: a member or device of the workgroup will deliver the key once it has been online;
// - `lost`: by the verified log no active principal other than an admin that reads only by inheritance can hold the key
//   (no explicit member, no assigned device, the author of the current epoch gone), so older content stays closed.
// Shown only once the org's job queue has run at least once and is idle (the wraps of this sign-in are fetched), so
// the line does not flash while the keys are still being read.
import { epochKey, type OrgState, WG } from '../log/state';
import { orgEntry } from '../store/slices/orgs';
import type { StoreState } from '../store/store';

export type Unlock = 'unlocking' | 'lost';

/** Whether anybody but `me` and inheriting admins can hold the key of the current epoch of `wg`. */
export function keyHolderLeft(state: OrgState, me: string, wg: string): boolean {
  const author = state.epochAuthors.get(epochKey(WG, wg, state.epoch(WG, wg)));
  return [...state.principals.values()].some(
    (p) =>
      p.id !== me &&
      ((p.id === author && state.member(p.id) !== null) ||
        state.assigned(p.id, wg) ||
        state.explicitAccess(p.id, wg) !== null),
  );
}

/** The state of `wg` for `me`: null if `me` cannot read it or holds the key of its current epoch. */
export function unlockStatus(state: OrgState, me: string, wg: string, held: boolean): Unlock | null {
  if (held || !state.canRead(me, WG, wg)) return null;
  return keyHolderLeft(state, me, wg) ? 'unlocking' : 'lost';
}

/** The selector of the views: `unlockStatus` once the org's log is verified and its job queue settled. */
export const selectUnlock =
  (org: string, wg: string) =>
  (store: StoreState): Unlock | null => {
    const entry = orgEntry(store.orgs, org);
    const me = store.me.userId;
    const queue = store.jobs[org];
    const settled = queue !== undefined && queue.last !== null && queue.running === null && queue.waiting === 0;
    if (entry.log === null || me === null || !settled) return null;
    const state = entry.log.state;
    return unlockStatus(state, me, wg, entry.keys.has(epochKey(WG, wg, state.epoch(WG, wg))));
  };
