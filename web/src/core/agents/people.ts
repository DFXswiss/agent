// Loading the situation view by person (06-web.md section 4.0a, ADR-095 section 5; 03-api.md section 4.6):
//
// - `loadPeople`: the person rows of an org, `GET /v1/orgs/{org}/people/agents?limit=50`, then `after` = the previous
//   page's `next_after`; `refresh` (after a reconnect, KOR-11) loads the first page again and starts the list over;
// - `loadMainAgents`: the open main agents of one person, `GET /v1/orgs/{org}/main-agents?owner=…&limit=50`, then
//   `before` = the previous page's `next_before`. The task rows enter the `tasks` slice like the rows of a task list (a
//   hub must not slip another org's task or another person's task into the list), the attention records the slice
//   `mainAgents`;
// - the order the view shows: the signed-in user's own row first, the others of the loaded pages by their shown name
//   (`orderPeople`, the names are known in the browser only); a person's agents newest activity first, closed ones
//   left out (a `task` frame may close one while the list is open; `selectAgentIds`);
// - the attention record of an agent (`selectAgentAttention`): the newer of the situation view's record (live
//   `attention` frames) and the one the page brought.
// Failures are recorded in the list, never thrown; a list that is loading, or has no further page, is left alone.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { type AttentionRow, isAttentionRow, staleRow } from '../store/slices/attention';
import {
  agentList,
  agentsFailed,
  agentsLoading,
  agentsPage,
  peopleFailed,
  peopleList,
  peopleLoading,
  peoplePage,
} from './state';
import type { PersonRow } from '../store/slices/mainAgents';
import { isTaskRow, newestFirst, putTasks, type TaskRow, type TasksState } from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import { sendErrorCode } from '../tasks/send';

/** Rows per page (the hub's default; the view's window is as large). */
export const PAGE_SIZE = 50;

const COUNTS = ['agents', 'needs_decision', 'blocked', 'working', 'subagents_active'] as const;
const isCount = (value: unknown): boolean => Number.isSafeInteger(value) && (value as number) >= 0;

export function isPersonRow(value: unknown): value is PersonRow {
  return (
    isRecord(value) &&
    typeof value.owner_id === 'string' &&
    value.owner_id !== '' &&
    COUNTS.every((field) => isCount(value[field]))
  );
}

/** The request target of a page of person rows. */
export function peopleTarget(org: string, after: string | null): string {
  const params = new URLSearchParams();
  if (after !== null) params.set('after', after);
  params.set('limit', String(PAGE_SIZE));
  return `/v1/orgs/${encodeURIComponent(org)}/people/agents?${params.toString()}`;
}

/** The request target of a page of one person's main agents. */
export function agentsTarget(org: string, owner: string, before: string | null): string {
  const params = new URLSearchParams({ owner });
  if (before !== null) params.set('before', before);
  params.set('limit', String(PAGE_SIZE));
  return `/v1/orgs/${encodeURIComponent(org)}/main-agents?${params.toString()}`;
}

/** Loads the first page (`more` false) or the next page of the person rows; returns the number of rows it brought. */
export async function loadPeople(context: TasksContext, org: string, more = false, refresh = false): Promise<number> {
  const { store } = context;
  const list = peopleList(store.getState().mainAgents, org);
  if (!refresh && (list.status === 'loading' || (more && list.nextAfter === null))) return 0;
  const generation = store.generation;
  if (!refresh) peopleLoading(store, org);
  try {
    const body = await context.client.request('GET', peopleTarget(org, more ? list.nextAfter : null));
    const valid =
      isRecord(body) &&
      Array.isArray(body.people) &&
      body.people.every(isPersonRow) &&
      (body.next_after === null || typeof body.next_after === 'string');
    if (!valid) throw new HubError('unavailable', 200);
    if (store.generation !== generation) return 0;
    const rows = body.people as PersonRow[];
    peoplePage(store, { org, rows, nextAfter: body.next_after as string | null, first: !more });
    return rows.length;
  } catch (error) {
    if (store.generation === generation && !refresh) peopleFailed(store, org, sendErrorCode(error));
    return 0;
  }
}

interface Item {
  readonly task: TaskRow;
  readonly attention: AttentionRow | null;
}

const isItem = (value: unknown): value is Item =>
  isRecord(value) &&
  isTaskRow(value.task) &&
  (value.attention === null || (isAttentionRow(value.attention) && value.attention.task_id === value.task.task_id));

/**
 * Loads the first page (`more` false) or the next page of the main agents of `owner`; returns the number of agents
 * the page brought.
 */
export async function loadMainAgents(
  context: TasksContext,
  org: string,
  owner: string,
  more = false,
  refresh = false,
): Promise<number> {
  const { store } = context;
  const list = agentList(store.getState().mainAgents, org, owner);
  if (!refresh && (list.status === 'loading' || (more && list.nextBefore === null))) return 0;
  const generation = store.generation;
  if (!refresh) agentsLoading(store, org, owner);
  try {
    const body = await context.client.request('GET', agentsTarget(org, owner, more ? list.nextBefore : null));
    const valid =
      isRecord(body) &&
      Array.isArray(body.items) &&
      body.items.every(isItem) &&
      (body.next_before === null || typeof body.next_before === 'string');
    if (!valid) throw new HubError('unavailable', 200);
    if (store.generation !== generation) return 0;
    const items = (body.items as Item[]).filter((item) => item.task.org_id === org && item.task.owner_id === owner);
    putTasks(
      store,
      items.map((item) => item.task),
    );
    agentsPage(store, {
      org,
      owner,
      ids: items.map((item) => item.task.task_id),
      attention: items.flatMap((item) => (item.attention === null ? [] : [item.attention])),
      nextBefore: body.next_before as string | null,
      first: !more,
    });
    return items.length;
  } catch (error) {
    if (store.generation === generation && !refresh) agentsFailed(store, org, owner, sendErrorCode(error));
    return 0;
  }
}

/** The owners in the order of the view: `me` first, the others by `name`, then by ID. */
export function orderPeople(owners: readonly string[], me: string | null, name: (owner: string) => string): string[] {
  const rank = (owner: string) => Number(owner !== me);
  return [...owners].sort(
    (a, b) => rank(a) - rank(b) || name(a).localeCompare(name(b)) || Number(a > b) - Number(a < b),
  );
}

const NO_IDS: readonly string[] = [];

/** A selector of the open main agents of `owner` in `org`, newest activity first (the same array while unchanged). */
export function selectAgentIds(org: string, owner: string): (state: StoreState) => readonly string[] {
  let last: { ids: readonly string[]; tasks: TasksState['tasks']; shown: readonly string[] } | null = null;
  return (state) => {
    const { ids } = agentList(state.mainAgents, org, owner);
    const tasks = state.tasks.tasks;
    if (last !== null && last.ids === ids && last.tasks === tasks) return last.shown;
    const open = ids.filter((id) => tasks[id] !== undefined && tasks[id].state !== 'closed');
    const sorted = newestFirst(tasks, open);
    const shown = last !== null && sorted.join(' ') === last.shown.join(' ') ? last.shown : sorted;
    last = { ids, tasks, shown: shown.length === 0 ? NO_IDS : shown };
    return last.shown;
  };
}

/** The newer of the situation view's attention record of `task` and the one its page of main agents brought. */
export const selectAgentAttention =
  (task: string) =>
  (state: StoreState): AttentionRow | null => {
    const live = state.attention.rows[task];
    const paged = state.mainAgents.attention[task];
    if (live === undefined) return paged ?? null;
    return paged !== undefined && staleRow(paged, live) ? paged : live;
  };
