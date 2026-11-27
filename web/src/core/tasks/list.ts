// The task list of a workgroup (06-web.md section 5.1, 03-api.md section 4.6): `GET /v1/workgroups/{wg}/tasks`
// newest first by latest activity (`order=activity`, KOR-32) with optional hub filters (state and the ticket fields of
// ADR-102, `filters.ts`; `filter` is the list's key), page by page (`before` = the previous page's `next_before`, infinite
// scroll). Rows enter the `tasks` slice; later changes arrive as `task` frames and move rows between the loaded lists.
// Titles are encrypted: `titles` pulls the `task.meta` events of tasks whose title is not in the store yet (newest
// pages per workgroup, bounded per call).
import { eventsBefore, NEWEST, PAGE_MAX } from '../net/pages';
import { setPage } from '../store/slices/pages';
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
import type { Store, StoreState } from '../store/store';
import { type PagedList, pagesKey } from './paged-list';
import type { TasksContext } from './pull';
import { selectTitle, TITLE_KIND } from './directory';
import { filterParams } from './filters';
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
  filterParams(filter, params);
  if (before !== null) params.set('before', before);
  params.set('limit', String(PAGE_SIZE));
  // the order of the key (ADR-103 section 2), else the latest activity (KOR-32)
  if (!params.has('order')) params.set('order', 'activity');
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

/** Older `task.meta` pages one `titles` call reads per workgroup at most (ADR-086: bounded, never every page). */
export const TITLE_PAGES = 10;

/** A task whose title is wanted. */
export type TitleTarget = Pick<TaskRow, 'org_id' | 'wg_id' | 'task_id' | 'title_event_id'>;

interface TitleQueue {
  readonly generation: number;
  /** The running read (reads of one workgroup run one after another). */
  tail: Promise<void>;
}

const titleQueues = new WeakMap<Store, Map<string, TitleQueue>>();

/** The paged list of a workgroup's title events (its `before` in the `pages` slice is where the next read goes on). */
export const titleList = (org: string, wg: string): PagedList => ({ org, scope: wg, query: { kinds: [TITLE_KIND] } });

/**
 * Pulls the title events of tasks whose title is not in the store, once per list (ADR-086 section 1): per workgroup
 * the `task.meta` pages newest first, continuing where the previous call stopped, while a wanted title is missing –
 * at most `TITLE_PAGES` pages per call, never a pull per row. Where the next read goes on is the list's `before` in the
 * `pages` slice: the event store raises it above an evicted title (`slices/events.ts`), so an evicted title is read
 * again. Later titles arrive over the live channel. Failed pulls only leave titles missing (the deterministic title is
 * shown).
 */
export async function titles(context: TasksContext, rows: readonly TitleTarget[]): Promise<void> {
  const { store } = context;
  const missing = (wg: string) =>
    rows.some(
      (row) =>
        row.wg_id === wg &&
        selectTitle(row.wg_id, row.task_id, row.title_event_id)(store.getState()).status === 'missing',
    );
  const scopes = new Map(rows.filter((row) => missing(row.wg_id)).map((row) => [row.wg_id, row.org_id]));
  const queues = titleQueues.get(store) ?? new Map<string, TitleQueue>();
  titleQueues.set(store, queues);
  await Promise.all(
    [...scopes].map(([wg, org]) => {
      let queue = queues.get(wg);
      if (queue === undefined || queue.generation !== store.generation) {
        queue = { generation: store.generation, tail: Promise.resolve() };
        queues.set(wg, queue);
      }
      const current = queue;
      const list = titleList(org, wg);
      const key = pagesKey(list);
      // where the next read goes on: NEWEST before the first, null when every page was read
      const next = (): number | null => {
        const page = store.getState().pages[key];
        return page === undefined ? NEWEST : page.before;
      };
      current.tail = current.tail
        .then(async () => {
          for (let page = 0; page < TITLE_PAGES && next() !== null && missing(wg); page += 1) {
            const before = next()!;
            const read = await eventsBefore(context.client, wg, list.query, before, PAGE_MAX);
            if (store.generation !== current.generation) return;
            // the cursor moves on before the events arrive (an eviction meanwhile raises it again)
            const known = store.getState().pages[key];
            if (known === undefined || known.before === before) {
              setPage(store, key, { status: 'ready', list: { scope: wg, query: list.query }, before: read.nextBefore });
            }
            await context.ingest(org, wg, read.events);
          }
        })
        .catch(() => undefined);
      return current.tail;
    }),
  );
}
