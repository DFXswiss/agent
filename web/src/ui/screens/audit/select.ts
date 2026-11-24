// List-level selectors of the audit-like lists (ADR-063 section 1): one `useStore` per list delivers what its rows
// need – task titles, agent names – recomputed only when the store parts they read change, so rows never subscribe to
// the store themselves.
import type { AuditRow } from '../../../core/monitoring/audit';
import type { Selector, StoreState } from '../../../core/store';
import { agentName } from '../../../core/store/slices/agents';
import { selectTitle } from '../../../core/tasks/directory';

/**
 * A selector that runs `compute` only when one of the values `inputs` reads from the state changed (by identity), and
 * otherwise returns the previous result: a stable value for `useStore`.
 */
export function cachedSelector<T>(
  inputs: (state: StoreState) => readonly unknown[],
  compute: (state: StoreState) => T,
): Selector<T> {
  let known: readonly unknown[] | null = null;
  let value: T;
  return (state) => {
    const now = inputs(state);
    if (known === null || now.some((input, index) => input !== known![index])) {
      known = now;
      value = compute(state);
    }
    return value;
  };
}

/** Whether two plain objects hold the same values (one level). */
export function shallowSame<T extends object>(a: T, b: T): boolean {
  const keys = Object.keys(a) as (keyof T)[];
  return keys.every((key) => a[key] === b[key]);
}

export interface TaskRef {
  readonly wg: string;
  readonly task: string;
}

/** The distinct tasks (with their workgroup) the rows refer to, in row order. */
export function tasksOf(rows: readonly AuditRow[]): TaskRef[] {
  const seen = new Map<string, TaskRef>();
  for (const row of rows) {
    if (row.wg_id !== null && row.task_id !== null && !seen.has(row.task_id))
      seen.set(row.task_id, { wg: row.wg_id, task: row.task_id });
  }
  return [...seen.values()];
}

/** Task ID → verified title of the given tasks; recomputed only when one of their workgroup scopes changed. */
export function titlesSelector(tasks: readonly TaskRef[]): Selector<ReadonlyMap<string, string>> {
  return cachedSelector(
    (state) => tasks.map((ref) => state.events[ref.wg]),
    (state) => {
      const titles = new Map<string, string>();
      for (const ref of tasks) {
        const title = selectTitle(ref.wg, ref.task, null)(state);
        if (title.status === 'verified') titles.set(ref.task, title.text);
      }
      return titles;
    },
  );
}

/** The distinct agent IDs of the rows. */
export function agentsOf(rows: readonly AuditRow[]): string[] {
  return [...new Set(rows.flatMap((row) => (row.agent_id === undefined ? [] : [row.agent_id])))];
}

/** Agent ID → name of the user's own agent (null for another agent); recomputed when the `agents` slice changed. */
export function agentNamesSelector(ids: readonly string[]): Selector<ReadonlyMap<string, string | null>> {
  return cachedSelector(
    (state) => [state.agents],
    (state) => new Map(ids.map((id) => [id, agentName(state, id)])),
  );
}
