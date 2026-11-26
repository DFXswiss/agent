// The task overview of an organization (KOR-32, route `/o/:org/tasks`): every task of the workgroups the user can
// read, newest activity first. The hub lists tasks per workgroup only (`GET /v1/workgroups/{wg}/tasks`, right
// `wg:read`), so the overview loads the list of each readable workgroup – the readable set comes from the verified
// membership log, a workgroup the user cannot read is never requested – and merges them (k-way merge):
//
// - Every workgroup list is ordered by latest activity (`order=activity`) and paged with its own cursor; the lists are
//   the same store lists as the task list of a single workgroup (`core/tasks/list.ts`), one per workgroup and state.
// - A merged row is shown only when no unloaded row of another list can come before it: the cut-off is the newest
//   "last loaded row" among the lists that have a further page. Rows below it wait for those pages, so paging never
//   shows a row in the wrong place and never skips one.
// - "Load more" loads the next page of the list that holds the cut-off (the binding list) until at least one page more
//   of merged rows is shown or no list has a further page.
// - "Only mine" (the signed owner or an agent of the user, never the hub's `owner_id`) and the title search run in
//   the browser over the merged rows, because titles are encrypted; so do the GitHub organisation and repository of
//   a captured task (sealed in its `task.meta`, ADR-100 section 2). Status, urgency, person and topic are hub filters
//   (the list key, `filters.ts`).
import { parseTime } from '../encoding/time';
import { type OrgState, WG } from '../log/state';
import { workgroupOrder } from '../org/members';
import { orgEntry } from '../store/slices/orgs';
import { type TaskList, taskList, type TaskRow, type TasksState } from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import { selectOwner, selectTitle } from './directory';
import { selectMeta } from './tickets';
import { loadTasks, PAGE_SIZE } from './list';
import type { TasksContext } from './pull';

/** The workgroups of `org` whose tasks `user` can read (default first, then active, then archived; verified log). */
export function readableWorkgroups(state: OrgState, user: string): string[] {
  return workgroupOrder(state).filter((wg) => state.canRead(user, WG, wg));
}

export interface OverviewQuery {
  readonly org: string;
  /** The workgroups to merge (readable ones only, see `readableWorkgroups`). */
  readonly wgs: readonly string[];
  /** The state filter (hub side), null for all states. */
  readonly state: string | null;
  /** Only tasks owned by this user (null: everyone's). */
  readonly mine: string | null;
  /** Title search (case-insensitive part of the title; empty: no search). */
  readonly search: string;
  /** GitHub organisation of the sealed `task.meta` (case-insensitive, whole login; empty or absent: any; ADR-100). */
  readonly githubOrg?: string;
  /** Repository `owner/name` of the sealed `task.meta` (case-insensitive, whole name; empty or absent: any). */
  readonly githubRepo?: string;
}

/** Whether the browser-side filters of `query` are set (search, only mine, GitHub organisation or repository). */
export const clientFiltered = (query: Pick<OverviewQuery, 'mine' | 'search' | 'githubOrg' | 'githubRepo'>): boolean =>
  query.mine !== null ||
  query.search.trim() !== '' ||
  (query.githubOrg ?? '').trim() !== '' ||
  (query.githubRepo ?? '').trim() !== '';

export interface Overview {
  /** `loading` while a list has not answered its first page yet. */
  readonly status: 'loading' | 'ready';
  /** The rows in merged order up to the cut-off, before the browser-side filters (titles are pulled for these). */
  readonly merged: readonly TaskRow[];
  /** The rows to show: `merged` after "only mine" and the title search. */
  readonly rows: readonly TaskRow[];
  /** A list has a further page (the binding list, null if none). */
  readonly binding: string | null;
  /** A list is loading (first or further page). */
  readonly busy: boolean;
  /** The workgroups whose list failed, with the error code of the first one. */
  readonly failed: readonly string[];
  readonly code: string | null;
}

/** The order key of a row: latest activity, then task ID (both descending). */
const after = (a: TaskRow, b: TaskRow): boolean => {
  const delta = parseTime(a.last_activity_at) - parseTime(b.last_activity_at);
  return delta < 0 || (delta === 0 && a.task_id < b.task_id);
};

/** The user a task belongs to: its owner, or the user of an owner that is an agent (null while unknown). */
function mineOf(state: StoreState, org: string, row: TaskRow): string | null {
  const owner = selectOwner(org, row.wg_id, row.task_id)(state);
  return owner === null ? null : orgEntry(state.orgs, org).log!.state.acting(owner);
}

/** A list that has not answered its first page yet. */
const pending = (list: TaskList): boolean =>
  list.status === 'idle' || (list.status === 'loading' && list.ids.length === 0 && list.nextBefore === null);

/** Normalized text for the title search. */
export const searchText = (text: string): string => text.normalize('NFKC').toLocaleLowerCase().trim();

/** The overview of `query` in `state` (pure; `selectOverview` memoizes it for React). */
export function overview(state: StoreState, query: OverviewQuery): Overview {
  const tasks: TasksState = state.tasks;
  const lists = query.wgs.map((wg) => taskList(tasks, wg, query.state));
  const failed = lists.filter((list) => list.status === 'error');
  const busy = lists.some((list) => list.status === 'loading');
  const base = { busy, failed: failed.map((list) => list.wg), code: failed[0]?.code ?? null };
  if (lists.some(pending)) return { status: 'loading', merged: [], rows: [], binding: null, ...base };
  // the cut-off: the newest last row among the lists with a further page (a list without rows binds everything)
  let binding: TaskList | null = null;
  let cut: TaskRow | null = null;
  for (const list of lists) {
    if (list.nextBefore === null) continue;
    const last = list.ids.length === 0 ? null : tasks.tasks[list.ids.at(-1)!]!;
    if (binding === null || (cut !== null && (last === null || after(cut, last)))) {
      binding = list;
      cut = last;
    }
  }
  const all = lists.flatMap((list) => list.ids.map((id) => tasks.tasks[id]!));
  const sorted = all.sort((a, b) => (after(a, b) ? 1 : -1));
  const merged = binding === null ? sorted : cut === null ? [] : sorted.filter((row) => !after(row, cut));
  const needle = searchText(query.search);
  const org = (query.githubOrg ?? '').trim().toLowerCase();
  const repo = (query.githubRepo ?? '').trim().toLowerCase();
  const rows = merged.filter((row) => {
    if (query.mine !== null && mineOf(state, query.org, row) !== query.mine) return false;
    if (org !== '' || repo !== '') {
      const meta = selectMeta(row.wg_id, row.task_id)(state);
      if (org !== '' && meta?.github_org.toLowerCase() !== org) return false;
      if (repo !== '' && meta?.github_repo.toLowerCase() !== repo) return false;
    }
    if (needle === '') return true;
    const title = selectTitle(row.wg_id, row.task_id, row.title_event_id)(state);
    return title.status === 'verified' && searchText(title.text).includes(needle);
  });
  return { status: 'ready', merged, rows, binding: binding?.wg ?? null, ...base };
}

const same = (a: readonly TaskRow[], b: readonly TaskRow[]): boolean =>
  a.length === b.length && a.every((row, index) => row === b[index]);

/**
 * A selector of the overview that returns the same object while nothing it shows changed (React's `useStore` needs
 * stable snapshots); create it once per query.
 */
export function selectOverview(query: OverviewQuery): (state: StoreState) => Overview {
  let last: Overview | null = null;
  return (state) => {
    const next = overview(state, query);
    const unchanged =
      last !== null &&
      last.status === next.status &&
      last.binding === next.binding &&
      last.busy === next.busy &&
      last.code === next.code &&
      last.failed.join() === next.failed.join() &&
      same(last.merged, next.merged) &&
      same(last.rows, next.rows);
    if (!unchanged) last = next;
    return last!;
  };
}

/** Loads the first page of every list (`refresh`: again after a reconnect, keeping further pages; KOR-11). */
export async function loadOverview(
  context: TasksContext,
  wgs: readonly string[],
  state: string | null,
  refresh = false,
): Promise<void> {
  await Promise.all(wgs.map((wg) => loadTasks(context, wg, state, false, refresh)));
}

/**
 * Loads further pages of the binding list until one page more of merged rows is shown, no list has a further page,
 * or a load brings nothing (a failure, or a load already running).
 */
export async function loadOverviewMore(context: TasksContext, query: OverviewQuery): Promise<void> {
  const target = overview(context.store.getState(), query).merged.length + PAGE_SIZE;
  for (;;) {
    const now = overview(context.store.getState(), query);
    if (now.binding === null || now.merged.length >= target) return;
    const rows = await loadTasks(context, now.binding, query.state, true);
    if (rows.length === 0) return;
  }
}
