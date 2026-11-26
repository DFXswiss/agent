// Hooks shared by the home screen, the task list and the task page: the verified org state with the user's rights,
// the org's names (profiles pulled once and read by the event reader) and task titles. A list reads the titles and
// owners of its rows with one selector (`useRowInfo`), never one store subscription per row.
import { useEffect, useMemo } from 'react';
import type { OrgState } from '../../../core/log/state';
import { orgEntry } from '../../../core/store/slices/orgs';
import type { TaskRow } from '../../../core/store/slices/tasks';
import type { StoreState } from '../../../core/store/store';
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

/** The owner of one task (the signed author of its first user-written `task.meta`, KOR-04), e.g. for a single card. */
export function useOwner(row: TaskRow): string | null {
  const { app } = useAppEnv();
  return useStore(selectOwner(row.org_id, row.wg_id, row.task_id), app.store);
}

/** What a list row shows of the store: the title and the owner (the signed author, not the hub's `owner_id`, KOR-04). */
export interface RowInfo {
  readonly title: Title;
  readonly owner: string | null;
}

export const sameTitle = (a: Title, b: Title): boolean =>
  (a.status === 'verified' && b.status === 'verified' && a.text === b.text) ||
  (a.status === 'unverifiable' && b.status === 'unverifiable' && a.code === b.code) ||
  (a.status === 'missing' && b.status === 'missing');

/**
 * One selector for the rows of a list: task ID → title and owner. It returns the same map while no row's title or
 * owner changed (a stable snapshot for `useStore`); create it once per row array.
 */
export function selectRowInfo(rows: readonly TaskRow[]): (state: StoreState) => ReadonlyMap<string, RowInfo> {
  let last: ReadonlyMap<string, RowInfo> | null = null;
  return (state) => {
    const next = new Map<string, RowInfo>();
    for (const row of rows) {
      next.set(row.task_id, {
        title: selectTitle(row.wg_id, row.task_id, row.title_event_id)(state),
        // the owner in the row's own organization (the dashboard lists several, ADR-101)
        owner: selectOwner(row.org_id, row.wg_id, row.task_id)(state),
      });
    }
    // the keys are the same in every call (the same rows): only the values can differ
    const unchanged =
      last !== null &&
      [...next].every(([id, info]) => {
        const old = last!.get(id)!;
        return old.owner === info.owner && sameTitle(old.title, info.title);
      });
    if (!unchanged) last = next;
    return last!;
  };
}

export function useRowInfo(rows: readonly TaskRow[]): ReadonlyMap<string, RowInfo> {
  const { app } = useAppEnv();
  const select = useMemo(() => selectRowInfo(rows), [rows]);
  return useStore(select, app.store);
}

/**
 * A selector of the directories of the organizations in `key` (IDs joined by commas, at least one): the same map
 * while every organization's directory is the same object (a stable snapshot for `useStore`). Create it once per key.
 */
export function selectDirectories(key: string): (state: StoreState) => ReadonlyMap<string, Directory> {
  let last: ReadonlyMap<string, Directory> | null = null;
  return (state) => {
    const next = new Map(key.split(',').map((org) => [org, selectDirectory(org)(state)]));
    if (last === null || [...next].some(([org, found]) => last!.get(org) !== found)) last = next;
    return last;
  };
}

/**
 * The names of several organizations (at least one; the task dashboard lists all of them, ADR-101); pulls their profiles
 * once.
 */
export function useDirectories(orgs: readonly string[]): ReadonlyMap<string, Directory> {
  const { app } = useAppEnv();
  const key = orgs.join(',');
  useEffect(() => {
    for (const org of key.split(',')) pullOnce(app.tasks, org, org, { kinds: PROFILE_KINDS }).catch(() => 0);
  }, [app, key]);
  const select = useMemo(() => selectDirectories(key), [key]);
  return useStore(select, app.store);
}

/** Pulls the titles of `rows` that are not in the store yet. */
export function useTitles(rows: readonly TaskRow[]): void {
  const { app } = useAppEnv();
  useEffect(() => {
    if (rows.length > 0) void titles(app.tasks, rows);
  }, [app, rows]);
}
