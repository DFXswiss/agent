// Slice `operations` (06-web.md sections 5.2 and 5.4, 03-api.md sections 4.7 and 5): operation rows as the hub reports
// them (`GET /v1/tasks/{task}/operations`, `GET /v1/operations/{id}`, live `operation` frames), the transitions of
// operations whose detail was loaded, and per task how far its list was paged. Rows carry metadata only: type, state,
// error code, the duplicate's original and the IDs of the encrypted `operation.params`/`operation.result` events.
//
// A row never goes back: rows carry no version, so a row whose state ranks below the stored one (a list answer
// overtaken by a frame) is ignored (`received` < decided < `executing` < final).
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

/** `operation_row` of the contracts (`contracts/api/objects.json`). */
export interface OperationRow {
  readonly operation_id: string;
  readonly org_id: string;
  readonly wg_id: string;
  readonly task_id: string;
  readonly session_id: string;
  readonly device_id: string;
  readonly actor_id: string;
  readonly type: string;
  readonly state: string;
  readonly error_code: string | null;
  readonly retryable: boolean;
  readonly duplicate_of: string | null;
  readonly attempt: number;
  readonly params_event_id: string | null;
  readonly result_event_id: string | null;
  readonly created_at: string;
  readonly decided_at: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  /** The GitHub identity the operation acted with (ADR-078 section 6); absent or null otherwise. */
  readonly github_identity?: string | null;
  /** The human whose instruction the operation followed (ADR-078 section 2); absent or null for older runners. */
  readonly triggered_by?: string | null;
}

/** `operation_transition`: `by_id` is the device, null for the hub's decision. */
export interface OperationTransition {
  readonly seq: number;
  readonly state: string;
  readonly error_code: string | null;
  readonly at: string;
  readonly by_id: string | null;
}

export interface TaskOperations {
  /** `next_before` of the last page, null when every page was read. */
  readonly nextBefore: string | null;
  readonly loaded: boolean;
}

export interface OperationsState {
  readonly rows: Readonly<Record<string, OperationRow>>;
  readonly transitions: Readonly<Record<string, readonly OperationTransition[]>>;
  readonly tasks: Readonly<Record<string, TaskOperations>>;
  /**
   * Counts the live channel's `hello` and `resync` frames: every (re)connect or resync may have lost `operation`
   * frames, so the operation lists are pulled again for each value (push is a hint, pull is the truth).
   */
  readonly round: number;
}

declare module '../store' {
  interface StoreState {
    operations: OperationsState;
  }
}

export const slice: Slice<'operations'> = {
  name: 'operations',
  initial: () => ({ rows: {}, transitions: {}, tasks: {}, round: 0 }),
};

const RANK: Readonly<Record<string, number>> = {
  received: 0,
  accepted: 1,
  duplicate: 1,
  denied: 1,
  rejected: 1,
  executing: 2,
  succeeded: 3,
  failed: 3,
  expired: 3,
};

/** Final states: nothing follows. */
export const FINAL_STATES: ReadonlySet<string> = new Set([
  'succeeded',
  'failed',
  'expired',
  'duplicate',
  'denied',
  'rejected',
]);

const ID_FIELDS = [
  'operation_id',
  'org_id',
  'wg_id',
  'task_id',
  'session_id',
  'device_id',
  'actor_id',
  'type',
  'state',
];
const NULLABLE_TEXT = [
  'error_code',
  'duplicate_of',
  'params_event_id',
  'result_event_id',
  'decided_at',
  'started_at',
  'finished_at',
];

export function isOperationRow(value: unknown): value is OperationRow {
  return (
    isRecord(value) &&
    ID_FIELDS.every((field) => typeof value[field] === 'string') &&
    NULLABLE_TEXT.every((field) => value[field] === null || typeof value[field] === 'string') &&
    typeof value.retryable === 'boolean' &&
    Number.isSafeInteger(value.attempt) &&
    typeof value.created_at === 'string' &&
    Object.hasOwn(RANK, value.state as string)
  );
}

export function isOperationTransition(value: unknown): value is OperationTransition {
  return (
    isRecord(value) &&
    Number.isSafeInteger(value.seq) &&
    typeof value.state === 'string' &&
    (value.error_code === null || typeof value.error_code === 'string') &&
    typeof value.at === 'string' &&
    (value.by_id === null || typeof value.by_id === 'string')
  );
}

/** Stores rows (module comment: never backwards). */
export function putOperations(store: Store, rows: readonly OperationRow[]): void {
  if (rows.length === 0) return;
  store.update('operations', (state) => {
    let changed = false;
    const next = { ...state.rows };
    for (const row of rows) {
      const known = next[row.operation_id];
      if (known !== undefined && RANK[known.state]! > RANK[row.state]!) continue;
      next[row.operation_id] = row;
      changed = true;
    }
    return changed ? { ...state, rows: next } : state;
  });
}

/** Stores the transitions of an operation (ascending by `seq`, one per `seq`). */
export function putTransitions(store: Store, operation: string, transitions: readonly OperationTransition[]): void {
  const unique = new Map(transitions.map((item) => [item.seq, item]));
  const sorted = [...unique.values()].sort((a, b) => a.seq - b.seq);
  store.update('operations', (state) => ({ ...state, transitions: { ...state.transitions, [operation]: sorted } }));
}

/** Records how far the task's list was paged. */
export function setTaskPage(store: Store, task: string, nextBefore: string | null): void {
  store.update('operations', (state) => ({
    ...state,
    tasks: { ...state.tasks, [task]: { nextBefore, loaded: true } },
  }));
}

/** Newest first: `created_at`, then the operation ID (UUIDv7, stable tie-breaker). */
export const newestFirst = (a: OperationRow, b: OperationRow): number =>
  Number(a.created_at < b.created_at) - Number(a.created_at > b.created_at) ||
  Number(a.operation_id < b.operation_id) - Number(a.operation_id > b.operation_id);

export const frames = {
  operation: (store: Store, data: unknown) => {
    if (isOperationRow(data)) putOperations(store, [data]);
  },
  hello: (store: Store) => store.update('operations', (state) => ({ ...state, round: state.round + 1 })),
  // a resync without reconnect may also have lost `operation` frames (KOR-05)
  resync: (store: Store) => store.update('operations', (state) => ({ ...state, round: state.round + 1 })),
};
