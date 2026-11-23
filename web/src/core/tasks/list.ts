// The task list of a workgroup (06-web.md section 5.1, 03-api.md section 4.6): `GET /v1/workgroups/{wg}/tasks`
// newest first by latest activity (`order=activity`, KOR-32) with an optional state filter, page by page (`before` = the previous page's `next_before`, infinite
// scroll). Rows enter the `tasks` slice; later changes arrive as `task` frames and move rows between the loaded lists.
// Titles are encrypted: `titles` pulls the `task.meta` events of tasks whose title is not in the store yet.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import {
  isTaskRow,
  listFailed,
  listLoading,
  listPage,
  type TaskList,
  type TaskRow,
  taskList,
  type TasksState,
} from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import type { TasksContext } from './pull';
import { selectTitle, TITLE_KIND } from './directory';
import { pullOnce } from './pull';
import { sendErrorCode } from './send';

/** Rows per page (the hub's default list size, 03-api.md section 1). */
export const PAGE_SIZE = 50;

const NO_ROWS: readonly TaskRow[] = [];
const rowsCache = new WeakMap<TaskList, { readonly tasks: TasksState['tasks']; readonly rows: readonly TaskRow[] }>();

/** The rows of a list in its order (the same array while neither the list nor the rows changed). */
export const selectListRows =
  (wg: string, filter: string | null) =>
  (state: StoreState): readonly TaskRow[] => {
    const list = taskList(state.tasks, wg, filter);
    if (list.ids.length === 0) return NO_ROWS;
    const cached = rowsCache.get(list);
    if (cached !== undefined && cached.tasks === state.tasks.tasks) return cached.rows;
    const rows = list.ids.map((id) => state.tasks.tasks[id]!);
    rowsCache.set(list, { tasks: state.tasks.tasks, rows });
    return rows;
  };

/** The request target of a page. */
export function listTarget(wg: string, filter: string | null, before: string | null): string {
  const params = new URLSearchParams();
  if (filter !== null) params.set('state', filter);
  if (before !== null) params.set('before', before);
  params.set('limit', String(PAGE_SIZE));
  params.set('order', 'activity');
  return `/v1/workgroups/${encodeURIComponent(wg)}/tasks?${params.toString()}`;
}

/**
 * Loads the first page (`more` false) or the next page of a list; a list that is loading, or that has no further
 * page, is left alone. Failures are recorded in the list (`status: error`, `code`), never thrown. `refresh` (after a
 * reconnect or resync of the live channel, KOR-11) loads the first page again even while another load runs (that one
 * may have been answered before the lost frames) and keeps the further pages already loaded and the list's state: a
 * failure leaves the rows as they are for the next round.
 */
export async function loadTasks(
  context: TasksContext,
  wg: string,
  filter: string | null,
  more = false,
  refresh = false,
): Promise<readonly TaskRow[]> {
  const { store } = context;
  const list = taskList(store.getState().tasks, wg, filter);
  if (!refresh && (list.status === 'loading' || (more && list.nextBefore === null))) return [];
  const generation = store.generation;
  if (!refresh) listLoading(store, wg, filter);
  try {
    const body = await context.client.request('GET', listTarget(wg, filter, more ? list.nextBefore : null));
    const valid =
      isRecord(body) &&
      Array.isArray(body.tasks) &&
      body.tasks.every(isTaskRow) &&
      (body.next_before === null || typeof body.next_before === 'string');
    if (!valid) throw new HubError('unavailable', 200);
    const rows = body.tasks as TaskRow[];
    if (store.generation !== generation) return [];
    listPage(store, { wg, state: filter, rows, nextBefore: body.next_before as string | null, first: !more, refresh });
    return rows;
  } catch (error) {
    if (store.generation === generation && !refresh) listFailed(store, wg, filter, sendErrorCode(error));
    return [];
  }
}

/**
 * Pulls the title events of tasks whose title is not in the store: first all `task.meta` events of each workgroup
 * once (one pull per workgroup and session), then, for tasks still without a title (created after that pull and missed
 * by the channel), the task's own `task.meta` events. Failed pulls only leave titles missing.
 */
export async function titles(context: TasksContext, rows: readonly TaskRow[]): Promise<void> {
  const missing = () =>
    rows.filter(
      (row) => selectTitle(row.wg_id, row.task_id, row.title_event_id)(context.store.getState()).status === 'missing',
    );
  const scopes = new Map(missing().map((row) => [row.wg_id, row.org_id]));
  await Promise.all([...scopes].map(([wg, org]) => pullOnce(context, org, wg, { kinds: [TITLE_KIND] }).catch(() => 0)));
  await Promise.all(
    missing().map((row) =>
      pullOnce(context, row.org_id, row.wg_id, { kinds: [TITLE_KIND], task: row.task_id }).catch(() => 0),
    ),
  );
}
