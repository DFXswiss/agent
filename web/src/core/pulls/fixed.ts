// "Conflicts fixed, branch updated" (KOR-48, ADR-084 section 5): the newest succeeded `github.push` of the task to the
// pull request's head branch whose pushed commit is a merge commit the task made (`github.commit` with `merge: true`
// in the same repository). Read from the task's decrypted operation events (`core/operations/view.ts`: verified, naming
// the operation, written by the operation's device when its row is known; the type of a known row must match the
// parameters); an event that fails counts as nothing.
import { operationView, type OperationView } from '../operations/view';
import type { EventEntry } from '../store/slices/events';
import type { OperationRow } from '../store/slices/operations';
import type { StoreState } from '../store/store';
import type { PullRef } from './address';

export interface Fixed {
  /** The merge commit on the branch. */
  readonly commit: string;
  /** The device's time of the push result. */
  readonly at: string;
}

interface Done {
  readonly type: string;
  readonly params: Readonly<Record<string, unknown>>;
  readonly result: Readonly<Record<string, unknown>>;
  readonly at: string;
}

/** The parameters and result of a succeeded operation whose row (when known) has the parameters' type. */
function done(view: OperationView): Done | null {
  if (view.params.status !== 'verified' || view.result.status !== 'verified') return null;
  const result = view.result.value.result;
  const type = view.params.value.type;
  if (result === null || (view.row !== null && view.row.type !== type)) return null;
  return { type, params: view.params.value.params, result, at: view.result.at };
}

const sameRepo = (value: unknown, ref: PullRef): boolean =>
  typeof value === 'string' && value.toLowerCase() === ref.repo.toLowerCase();

/** The fix of `ref` (branch `headRef`) among the operation events of `task` (module comment), or null. */
export function conflictsFixed(
  entries: Iterable<EventEntry>,
  rows: Readonly<Record<string, OperationRow>>,
  task: string,
  ref: PullRef,
  headRef: string,
): Fixed | null {
  const byOperation = new Map<string, EventEntry[]>();
  for (const entry of entries) {
    if (entry.status !== 'verified') continue;
    const header = entry.header!;
    if (header.task !== task || typeof header.ref !== 'string' || !String(header.kind).startsWith('operation.'))
      continue;
    const list = byOperation.get(header.ref) ?? [];
    list.push(entry);
    byOperation.set(header.ref, list);
  }
  const merges = new Set<string>();
  const pushes: Done[] = [];
  for (const [operation, list] of byOperation) {
    const found = done(operationView(list, operation, rows[operation] ?? null));
    if (found === null || !sameRepo(found.params.repo, ref)) continue;
    if (found.type === 'github.commit' && found.params.merge === true && typeof found.result.commit_sha === 'string')
      merges.add(found.result.commit_sha);
    if (found.type === 'github.push' && found.result.branch === headRef) pushes.push(found);
  }
  const fixes = pushes.filter((push) => typeof push.result.sha === 'string' && merges.has(push.result.sha));
  const newest = fixes.sort((a, b) => Date.parse(b.at) - Date.parse(a.at))[0];
  return newest === undefined ? null : { commit: newest.result.sha as string, at: newest.at };
}

/** A memoized selector of the fix of `ref` in `task` of workgroup `wg` (null without a head branch read yet). */
export function selectFixed(wg: string, task: string, ref: PullRef, headRef: string | null) {
  let last: { scope: unknown; rows: unknown; fixed: Fixed | null } | null = null;
  return (state: StoreState): Fixed | null => {
    if (headRef === null) return null;
    const scope = state.events[wg];
    const rows = state.operations.rows;
    if (last !== null && last.scope === scope && last.rows === rows) return last.fixed;
    const fixed = scope === undefined ? null : conflictsFixed(scope.entries.values(), rows, task, ref, headRef);
    last = { scope, rows, fixed };
    return fixed;
  };
}
