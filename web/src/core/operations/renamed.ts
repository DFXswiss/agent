// "Renamed by session" (06-web.md section 4.2, 03-api.md section 9 `task.set_title`): a session refines a task's title
// through the operation `task.set_title`; the runner writes a device-authored `task.meta` event. The task's newest
// verified `task.meta` decides: written by a device of the org (verified log) → the title was renamed by a session.
// The task row names its title event (`title_event_id`, the hub's newest at the time of the row); a newer verified
// title event than the row names means the row is outdated (`selectNewestTitle`), and the task page reloads it.
import { DEVICE } from '../log/state';
import { orgEntry } from '../store/slices/orgs';
import type { StoreState } from '../store/store';
import { TITLE_KIND } from '../tasks/directory';

export const selectRenamed =
  (org: string, wg: string, task: string) =>
  (state: StoreState): boolean => {
    let newest: { seq: number; author: unknown } | null = null;
    for (const entry of state.events[wg]?.entries.values() ?? []) {
      if (entry.status !== 'verified' || entry.header!.kind !== TITLE_KIND || entry.header!.task !== task) continue;
      if (newest === null || entry.scopeSeq > newest.seq)
        newest = { seq: entry.scopeSeq, author: entry.header!.author };
    }
    const principal = newest === null ? null : orgEntry(state.orgs, org).log?.state.member(newest.author as string);
    return principal?.kind === DEVICE;
  };

/** The ID of the task's newest verified `task.meta` event in the store, or null. */
export const selectNewestTitle =
  (wg: string, task: string) =>
  (state: StoreState): string | null => {
    let newest: { seq: number; id: string } | null = null;
    for (const entry of state.events[wg]?.entries.values() ?? []) {
      if (entry.status !== 'verified' || entry.header!.kind !== TITLE_KIND || entry.header!.task !== task) continue;
      if (newest === null || entry.scopeSeq > newest.seq)
        newest = { seq: entry.scopeSeq, id: entry.header!.id as string };
    }
    return newest?.id ?? null;
  };
