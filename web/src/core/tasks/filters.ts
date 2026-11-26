// The hub-side filters of a task list (03-api.md section 4.6, ADR-100 section 5): the state (06-web.md section 5.1)
// and the ticket fields status, urgency, person (`assignee`: a user ID or `none`) and topic (a topic ID or `none`) of
// `GET /v1/workgroups/{wg}/tasks`. A list of the `tasks` slice is keyed by one text (`filterKey`): null for every task,
// the state alone for a state filter (the key of before), else the query of the set filters in a fixed order. Changed
// rows are placed into the loaded lists whose filter they match (`matchesFilter`). No runtime imports: the store
// slice uses it.
import type { TaskRow } from '../store/slices/tasks';

/** The filters the hub applies; null or absent: not filtered. `order` is the list's order (E-43, ADR-101 section 2;
 * absent: latest activity). */
export interface HubFilter {
  readonly state?: string | null;
  readonly status?: string | null;
  readonly urgency?: string | null;
  readonly assignee?: string | null;
  readonly topic?: string | null;
  readonly order?: string | null;
}

const NAMES = ['state', 'status', 'urgency', 'assignee', 'topic', 'order'] as const;

/** The orders of a task list: latest activity (the default), newest creation, most urgent first. */
export const ORDERS = ['activity', 'created', 'urgency'] as const;
export type Order = (typeof ORDERS)[number];

/** The order of a list key (latest activity when it names none). */
export const orderOf = (key: string | null): Order => {
  const order = parseFilter(key).order;
  return order === 'created' || order === 'urgency' ? order : 'activity';
};

/** "No person", "no topic". */
export const NONE = 'none';

/** The key of a filter (module comment). */
export function filterKey(filter: HubFilter): string | null {
  const set = NAMES.filter((name) => filter[name] != null && filter[name] !== '');
  if (set.length === 0) return null;
  if (set.length === 1 && set[0] === 'state') return filter.state!;
  const params = new URLSearchParams();
  for (const name of set) params.set(name, filter[name]!);
  return params.toString();
}

/** The filter of a key (`filterKey`'s inverse). */
export function parseFilter(key: string | null): HubFilter {
  if (key === null) return {};
  if (!key.includes('=')) return { state: key };
  const params = new URLSearchParams(key);
  return Object.fromEntries(
    NAMES.flatMap((name): [string, string][] => (params.has(name) ? [[name, params.get(name)!]] : [])),
  ) as HubFilter;
}

/** The status of a row: the hub's, or derived from the state for an older hub (closed: completed). */
export function rowStatus(row: TaskRow): string {
  if (row.status === 'todo' || row.status === 'inProgress' || row.status === 'completed') return row.status;
  return row.state === 'closed' ? 'completed' : 'inProgress';
}

/** Whether a row belongs to the list of `key`. */
export function matchesFilter(key: string | null, row: TaskRow): boolean {
  const filter = parseFilter(key);
  const id = (value: string | null | undefined, wanted: string) => (wanted === NONE ? value == null : value === wanted);
  return (
    (filter.state == null || filter.state === row.state) &&
    (filter.status == null || filter.status === rowStatus(row)) &&
    (filter.urgency == null || filter.urgency === (row.urgency ?? 'normal')) &&
    (filter.assignee == null || id(row.assignee_id, filter.assignee)) &&
    (filter.topic == null || id(row.topic_id, filter.topic))
  );
}

/** The query parameters of a filter key for `GET /v1/workgroups/{wg}/tasks` (the order only with `withOrder`). */
export function filterParams(key: string | null, params: URLSearchParams, withOrder = true): void {
  const filter = parseFilter(key);
  for (const name of NAMES) {
    const value = filter[name];
    if (value != null && (withOrder || name !== 'order')) params.set(name, value);
  }
}

const URGENCY_RANK: Readonly<Record<string, number>> = { low: 0, normal: 1, high: 2, urgent: 3 };

/**
 * Whether row `a` comes after row `b` in the order of `order` (every order descending, the task ID last): the hub's
 * order of each list, so the merge of several lists keeps it.
 */
export function comesAfter(order: Order, a: TaskRow, b: TaskRow): boolean {
  const time = (row: TaskRow) => Date.parse(order === 'created' ? row.created_at : row.last_activity_at);
  const rank = (row: TaskRow) => URGENCY_RANK[row.urgency ?? 'normal'] ?? 1;
  const byRank = order === 'urgency' ? rank(a) - rank(b) : 0;
  if (byRank !== 0) return byRank < 0;
  const delta = time(a) - time(b);
  return delta < 0 || (delta === 0 && a.task_id < b.task_id);
}
