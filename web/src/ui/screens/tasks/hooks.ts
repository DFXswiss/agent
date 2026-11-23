// Hooks shared by the home screen, the task list and the task page: the verified org state with the user's rights,
// the org's names (profiles pulled once and read by the event reader) and task titles.
import { useEffect } from 'react';
import type { OrgState } from '../../../core/log/state';
import { orgEntry } from '../../../core/store/slices/orgs';
import type { TaskRow } from '../../../core/store/slices/tasks';
import {
  type Directory,
  PROFILE_KINDS,
  selectDirectory,
  selectOwner,
  selectTitle,
  type Title,
} from '../../../core/tasks/directory';
import { titles } from '../../../core/tasks/list';
import { pullOnce } from '../../../core/tasks/pull';
import { useStore } from '../../hooks/useStore';
import { useAppEnv } from '../../shell/context';

export interface OrgView {
  /** The verified membership state, null while the log is not verified yet. */
  readonly state: OrgState | null;
  readonly userId: string | null;
}

export function useOrgView(org: string): OrgView {
  const { app } = useAppEnv();
  const log = useStore((state) => orgEntry(state.orgs, org).log, app.store);
  const userId = useStore((state) => state.me.userId, app.store);
  return { state: log?.state ?? null, userId };
}

/** The names of the org; pulls its profile events once the log is verified. */
export function useDirectory(org: string, ready: boolean): Directory {
  const { app } = useAppEnv();
  useEffect(() => {
    if (ready) pullOnce(app.tasks, org, org, { kinds: PROFILE_KINDS }).catch(() => 0);
  }, [app, org, ready]);
  return useStore(selectDirectory(org), app.store);
}

export function useTitle(row: TaskRow): Title {
  const { app } = useAppEnv();
  return useStore(selectTitle(row.wg_id, row.task_id, row.title_event_id), app.store);
}

/** The task's owner: the signed author of its first user-written `task.meta`, not the hub's `owner_id` (KOR-04). */
export function useOwner(row: TaskRow): string | null {
  const { app } = useAppEnv();
  return useStore(selectOwner(row.org_id, row.wg_id, row.task_id), app.store);
}

/** Pulls the titles of `rows` that are not in the store yet. */
export function useTitles(rows: readonly TaskRow[]): void {
  const { app } = useAppEnv();
  useEffect(() => {
    if (rows.length > 0) void titles(app.tasks, rows);
  }, [app, rows]);
}
