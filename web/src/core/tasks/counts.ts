// The counters of the task dashboard (ADR-101 section 1): per status and per urgency the number of tasks of the
// readable workgroups, from `GET /v1/orgs/{org}/tasks/counts` of every organization shown, summed. The hub filters of
// the list apply (state, person, topic; one workgroup); a status counter ignores the status filter and an urgency
// counter the urgency filter, so a click on a counter gives that many rows. The sealed filters (title search, "only
// mine", GitHub organisation and repository) narrow only the list, never the counters.
import { isRecord } from '../registry';
import { HubError } from '../net/signedFetch';
import type { StoreState } from '../store/store';
import { filterParams, rowStatus } from './filters';
import type { TasksContext } from './pull';
import { STATUSES, type TaskStatus, URGENCIES, type Urgency } from './tickets';

export interface TaskCounts {
  readonly byStatus: Readonly<Record<TaskStatus, number>>;
  readonly byUrgency: Readonly<Record<Urgency, number>>;
  readonly total: number;
}

export const NO_COUNTS: TaskCounts = {
  byStatus: { todo: 0, inProgress: 0, completed: 0 },
  byUrgency: { low: 0, normal: 0, high: 0, urgent: 0 },
  total: 0,
};

const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

const countsOf = (value: unknown, names: readonly string[]): Record<string, number> | null =>
  isRecord(value) && names.every((name) => isCount(value[name]))
    ? Object.fromEntries(names.map((name) => [name, value[name] as number]))
    : null;

/** The request target of one organization's counters for the list key `filter` (and one workgroup, `wg`). */
export function countsTarget(org: string, filter: string | null, wg: string | null): string {
  const params = new URLSearchParams();
  filterParams(filter, params, false);
  if (wg !== null) params.set('wg', wg);
  const target = `/v1/orgs/${encodeURIComponent(org)}/tasks/counts`;
  const query = params.toString();
  return query === '' ? target : `${target}?${query}`;
}

/** One organization's counters (`unavailable` for an answer of another form). */
export async function loadCounts(
  context: Pick<TasksContext, 'client'>,
  org: string,
  filter: string | null,
  wg: string | null,
): Promise<TaskCounts> {
  const body = await context.client.request('GET', countsTarget(org, filter, wg));
  if (!isRecord(body) || !isCount(body.total)) throw new HubError('unavailable', 200);
  const byStatus = countsOf(body.by_status, STATUSES);
  const byUrgency = countsOf(body.by_urgency, URGENCIES);
  if (byStatus === null || byUrgency === null) throw new HubError('unavailable', 200);
  return { byStatus, byUrgency, total: body.total } as TaskCounts;
}

/**
 * A selector of what the counters depend on among the loaded rows of the workgroups in `key` (IDs joined by commas):
 * each row's ID, status and urgency. A text, so it changes only when a row appears or its status or urgency changes
 * (a live row frame), and the counters are loaded again then – not on every page of the list.
 */
export function selectCountsSignature(key: string): (state: StoreState) => string {
  const wgs = new Set(key === '' ? [] : key.split(','));
  return (state) =>
    Object.values(state.tasks.tasks)
      .filter((row) => wgs.has(row.wg_id))
      .map((row) => `${row.task_id}:${rowStatus(row)}:${row.urgency ?? 'normal'}`)
      .sort()
      .join();
}

/** The sum of several organizations' counters. */
export function sumCounts(all: readonly TaskCounts[]): TaskCounts {
  const sum = <K extends string>(names: readonly K[], pick: (item: TaskCounts) => Readonly<Record<K, number>>) =>
    Object.fromEntries(names.map((name) => [name, all.reduce((total, item) => total + pick(item)[name], 0)])) as Record<
      K,
      number
    >;
  return {
    byStatus: sum(STATUSES, (item) => item.byStatus),
    byUrgency: sum(URGENCIES, (item) => item.byUrgency),
    total: all.reduce((total, item) => total + item.total, 0),
  };
}
