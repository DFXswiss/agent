// Loading the situation view (06-web.md section 4.0, 03-api.md section 4.6, ADR-076 section 4):
//
// - `loadGroup` / `loadGroupMore`: the pages of one group (`GET /v1/orgs/{org}/attention?category=…&limit=50`, then
//   `before` = the previous page's `next_before`); "Recently finished" asks for the last 7 days (`since`). The task
//   rows of a page enter the `tasks` slice like the rows of a task list. Failures are recorded in the group, never
//   thrown. `refresh` (after a reconnect, KOR-11) loads the first page again and keeps the further pages. The list
//   `no_agent` of the start page's overview asks with `overview=no_agent` instead of a category.
// - `refreshSummary`: the counters (`…/attention/summary?since=<local midnight>`), at most once per `SUMMARY_MS` per
//   org: a call inside that time is answered by one load at its end (a burst of frames costs one request a second).
// - `ensureTask`: the row of a task a live frame named before its row was known (`GET /v1/tasks/{task}`), once per
//   store generation.
// - `loadQuestion`: the operation behind a `question` / `approval` record (its row and its sealed `operation.params`
//   event, `GET /v1/scopes/{wg}/events?kinds=operation.params&task=…&ref=…`), read by the event reader like every
//   event (`core/operations/view.ts` checks it: verified, names the operation, written by the operation's device).
import { formatTime } from '../encoding/time';
import { HubError } from '../net/signedFetch';
import { loadOperation } from '../operations/load';
import { isRecord } from '../registry';
import {
  attentionGroup,
  type AttentionRow,
  groupFailed,
  groupLoading,
  groupPage,
  isAttentionRow,
  isSummary,
  type ListKind,
  putSummary,
} from '../store/slices/attention';
import { isTaskRow, putTasks, type TaskRow } from '../store/slices/tasks';
import { pullOnce, type TasksContext } from '../tasks/pull';
import { loadTask, sendErrorCode } from '../tasks/send';

/** Records per page (the hub's default and the view's step). */
export const PAGE_SIZE = 50;
/** "Recently finished" covers this long (06-web.md section 4.0). */
export const DONE_DAYS = 7;
/** The counters are refetched at most this often (ADR-076 section 4). */
export const SUMMARY_MS = 1000;

const DAY_MS = 86_400_000;

/** Local midnight of the day of `now` (the start of "Finished today"). */
export function localMidnight(now: number): number {
  const date = new Date(now);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

/** The request target of a page. */
export function groupTarget(org: string, category: ListKind, before: string | null, now: number): string {
  const params = new URLSearchParams(category === 'no_agent' ? { overview: category } : { category });
  if (category === 'done') params.set('since', formatTime(now - DONE_DAYS * DAY_MS));
  if (before !== null) params.set('before', before);
  params.set('limit', String(PAGE_SIZE));
  return `/v1/orgs/${encodeURIComponent(org)}/attention?${params.toString()}`;
}

interface Item {
  readonly task: TaskRow;
  readonly attention: AttentionRow;
}

const isItem = (value: unknown): value is Item =>
  isRecord(value) &&
  isTaskRow(value.task) &&
  isAttentionRow(value.attention) &&
  value.task.task_id === value.attention.task_id;

/**
 * Loads the first page (`more` false) or the next page of a group; a group that is loading, or has no further page,
 * is left alone (except for `refresh`). Returns the number of records the page brought.
 */
export async function loadGroup(
  context: TasksContext,
  org: string,
  category: ListKind,
  more = false,
  refresh = false,
): Promise<number> {
  const { store } = context;
  const group = attentionGroup(store.getState().attention, org, category);
  if (!refresh && (group.status === 'loading' || (more && group.nextBefore === null))) return 0;
  const generation = store.generation;
  if (!refresh) groupLoading(store, org, category);
  try {
    const target = groupTarget(org, category, more ? group.nextBefore : null, context.now());
    const body = await context.client.request('GET', target);
    const valid =
      isRecord(body) &&
      Array.isArray(body.items) &&
      body.items.every(isItem) &&
      (body.next_before === null || typeof body.next_before === 'string');
    if (!valid) throw new HubError('unavailable', 200);
    if (store.generation !== generation) return 0;
    // a hub must not slip another org's task into the list
    const items = (body.items as Item[]).filter((item) => item.task.org_id === org);
    putTasks(
      store,
      items.map((item) => item.task),
    );
    const rows = items.map((item) => item.attention);
    groupPage(store, { org, category, rows, nextBefore: body.next_before as string | null, first: !more, refresh });
    return rows.length;
  } catch (error) {
    if (store.generation === generation && !refresh) groupFailed(store, org, category, sendErrorCode(error));
    return 0;
  }
}

/** The next page of a group (no-op while it loads or has no further page). */
export const loadGroupMore = (context: TasksContext, org: string, category: ListKind): Promise<number> =>
  loadGroup(context, org, category, true);

/** Loads the counters now; failures leave the old ones. */
export async function loadSummary(context: TasksContext, org: string): Promise<void> {
  const generation = context.store.generation;
  const since = formatTime(localMidnight(context.now()));
  const params = new URLSearchParams({ since });
  try {
    const body = await context.client.request(
      'GET',
      `/v1/orgs/${encodeURIComponent(org)}/attention/summary?${params.toString()}`,
    );
    if (isSummary(body) && context.store.generation === generation) putSummary(context.store, org, body);
  } catch {
    // the counters stay as they were; the next change or reconnect asks again
  }
}

interface Throttle {
  last: number;
  timer: ReturnType<typeof setTimeout> | null;
}

const throttles = new WeakMap<object, Map<string, Throttle>>();

/** The counters of `org`, at most once per `SUMMARY_MS` (module comment). */
export function refreshSummary(context: TasksContext, org: string): void {
  const byOrg = throttles.get(context.store) ?? new Map<string, Throttle>();
  throttles.set(context.store, byOrg);
  const throttle = byOrg.get(org) ?? { last: -Infinity, timer: null };
  byOrg.set(org, throttle);
  if (throttle.timer !== null) return;
  const wait = throttle.last + SUMMARY_MS - context.now();
  const run = () => {
    throttle.timer = null;
    throttle.last = context.now();
    void loadSummary(context, org);
  };
  if (wait <= 0) run();
  else throttle.timer = setTimeout(run, wait);
}

const tasks = new WeakMap<object, Map<string, Promise<unknown>>>();

/** The row of `task` once per store generation (a failure is forgotten, so the next caller tries again). */
export function ensureTask(context: TasksContext, task: string): Promise<unknown> {
  const store = context.store;
  const loads = tasks.get(store) ?? new Map<string, Promise<unknown>>();
  tasks.set(store, loads);
  const key = `${store.generation} ${task}`;
  const known = loads.get(key);
  if (known !== undefined) return known;
  const load = loadTask(context, task).catch(() => {
    loads.delete(key);
    return null;
  });
  loads.set(key, load);
  return load;
}

/** The operation of a `question` / `approval` record and its sealed parameters (module comment). */
export async function loadQuestion(context: TasksContext, task: TaskRow, operation: string): Promise<void> {
  await Promise.all([
    loadOperation(context, operation),
    pullOnce(context, task.org_id, task.wg_id, { kinds: ['operation.params'], task: task.task_id, ref: operation }),
  ]);
}
