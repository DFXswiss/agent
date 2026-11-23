// Loading operations (03-api.md sections 4.4 and 4.7): the task's list newest first (`GET /v1/tasks/{task}/operations`,
// paged with `next_before`), one operation with its transitions (`GET /v1/operations/{id}`), and the operation's
// encrypted events (`GET /v1/scopes/{wg}/events?ref=<operation>&kinds=operation.params,operation.result`, read by the
// event reader like every other event, never trusted unverified). `ensureOperations` loads a task's first page once
// per store generation and live-channel round (every reconnect may have lost `operation` frames; shared by the chat
// cards and the tab; a failure is forgotten so the next caller tries again). Answers that arrive after a sign-out
// write nothing.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { StoredEvent } from '../net/sse';
import {
  isOperationRow,
  isOperationTransition,
  type OperationRow,
  putOperations,
  putTransitions,
  setTaskPage,
} from '../store/slices/operations';
import type { TasksContext } from '../tasks/pull';

export const OPERATION_KINDS = ['operation.params', 'operation.result'] as const;
export const PAGE_LIMIT = 50;

/** One page of the task's operations (after `before`); stores rows and the paging position. */
export async function loadOperations(context: TasksContext, task: string, before: string | null = null): Promise<void> {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (before !== null) params.set('before', before);
  const generation = context.store.generation;
  const body = await context.client.request('GET', `/v1/tasks/${encodeURIComponent(task)}/operations?${params}`);
  if (
    !isRecord(body) ||
    !Array.isArray(body.operations) ||
    !body.operations.every(isOperationRow) ||
    !(body.next_before === null || typeof body.next_before === 'string')
  ) {
    throw new HubError('unavailable', 200);
  }
  if (context.store.generation !== generation) return;
  // a hub must not slip another task's operation into the list
  putOperations(
    context.store,
    body.operations.filter((row) => row.task_id === task),
  );
  setTaskPage(context.store, task, body.next_before);
}

const shared = new WeakMap<object, Map<string, Promise<void>>>();

/** The first page of `task` once per store generation (module comment). */
export function ensureOperations(context: TasksContext, task: string): Promise<void> {
  const store = context.store;
  const loads = shared.get(store) ?? new Map<string, Promise<void>>();
  shared.set(store, loads);
  const key = `${store.generation} ${store.getState().operations.round} ${task}`;
  const known = loads.get(key);
  if (known !== undefined) return known;
  const load = loadOperations(context, task).catch((error: unknown) => {
    loads.delete(key);
    throw error;
  });
  loads.set(key, load);
  return load;
}

/** One operation with its transitions. */
export async function loadOperation(context: TasksContext, operation: string): Promise<OperationRow> {
  const generation = context.store.generation;
  const body = await context.client.request('GET', `/v1/operations/${encodeURIComponent(operation)}`);
  if (
    !isRecord(body) ||
    !isOperationRow(body.operation) ||
    body.operation.operation_id !== operation ||
    !Array.isArray(body.transitions) ||
    !body.transitions.every(isOperationTransition)
  ) {
    throw new HubError('unavailable', 200);
  }
  if (context.store.generation !== generation) return body.operation;
  putOperations(context.store, [body.operation]);
  putTransitions(context.store, operation, body.transitions);
  return body.operation;
}

const isStored = (item: unknown): item is StoredEvent =>
  isRecord(item) &&
  Number.isSafeInteger(item.scope_seq) &&
  (item.received_at === null || typeof item.received_at === 'string') &&
  'event' in item;

/** Pulls the events of one operation (every page) into the reader; returns how many were read. */
export async function pullOperationEvents(context: TasksContext, row: OperationRow): Promise<number> {
  let after: number | null = null;
  let count = 0;
  for (;;) {
    const params = new URLSearchParams({ ref: row.operation_id, kinds: OPERATION_KINDS.join(','), limit: '1000' });
    if (after !== null) params.set('after', String(after));
    const page = await context.client.request('GET', `/v1/scopes/${encodeURIComponent(row.wg_id)}/events?${params}`);
    const events = isRecord(page) && Array.isArray(page.events) ? page.events : null;
    const next = isRecord(page) ? page.next_after : undefined;
    if (events === null || !events.every(isStored) || !(next === null || Number.isSafeInteger(next))) {
      throw new HubError('unavailable', 200);
    }
    await context.ingest(row.org_id, row.wg_id, events);
    count += events.length;
    if (next === null) return count;
    after = next as number;
  }
}
