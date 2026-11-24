// The queue of a task (06-web.md section 5.2, ADR-083 section 3, 03-api.md section 4.6):
//
// - `loadQueue`: `GET /v1/tasks/{task}/queue` - the prompts not yet delivered to a live main session, in event order,
//   each with its author, whether it came through the author's agent, and a state (`waiting`, `waiting_for_device`:
//   the session's device is offline or there is none, `withdrawing`), plus the task's withdrawals with their outcome
//   (`withdrawn`, `withdrawing`, `too_late`); into the `queue` slice. Rows of a shape this client does not know are
//   left out; a malformed answer is `unavailable`;
// - `withdrawPrompt`: `POST /v1/tasks/{task}/prompts/{event}/withdraw` (`task:write`, only the prompt's author) →
//   `withdrawn` (the session had no device) or `withdrawing` (the device decides); the queue shows the outcome at
//   once and is read again;
// - `watchQueue`: reads the queue when the chat opens and again, debounced, whenever the task row, its sessions or
//   its prompt events change (live frames and the reader write them to the store; the hub sends a `session` frame of
//   the holding session when the queue changed), and every `QUEUE_POLL_MS` while prompts wait. One read at a time;
//   nothing is written after a sign-out.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { EventEntry, ScopeEvents } from '../store/slices/events';
import {
  putQueue,
  type QueuedPrompt,
  type QueueState,
  type TaskQueue,
  type Withdrawal,
  type WithdrawalState,
} from '../store/slices/queue';
import type { SessionEntry, TaskRow, TasksState } from '../store/slices/tasks';
import { memoize } from '../store/selectors';
import { type StoreState, subscribe } from '../store/store';
import { requestErrorCode, type RequestsContext } from '../requests/client';

/** Pause after the last change before the queue is read again. */
export const QUEUE_DEBOUNCE_MS = 400;
/** Fallback read while prompts wait. */
export const QUEUE_POLL_MS = 30_000;

export const QUEUE_STATES: ReadonlySet<string> = new Set<QueueState>(['waiting', 'waiting_for_device', 'withdrawing']);
export const WITHDRAWAL_STATES: ReadonlySet<string> = new Set<WithdrawalState>([
  'withdrawn',
  'withdrawing',
  'too_late',
]);

const text = (value: unknown): value is string => typeof value === 'string';

function queuedPrompt(row: unknown): QueuedPrompt | null {
  if (!isRecord(row)) return null;
  const { event_id, author_id, via_agent, scope_seq, received_at, session_id, device_id, state } = row;
  if (!text(event_id) || !text(author_id) || !text(received_at) || !text(session_id)) return null;
  if (typeof via_agent !== 'boolean' || !Number.isSafeInteger(scope_seq)) return null;
  if (!(device_id === null || text(device_id)) || !text(state) || !QUEUE_STATES.has(state)) return null;
  return {
    eventId: event_id,
    authorId: author_id,
    viaAgent: via_agent,
    scopeSeq: scope_seq as number,
    receivedAt: received_at,
    sessionId: session_id,
    deviceId: device_id,
    state: state as QueueState,
  };
}

function withdrawal(row: unknown): Withdrawal | null {
  if (!isRecord(row) || !text(row.event_id) || !text(row.requested_at)) return null;
  if (!text(row.state) || !WITHDRAWAL_STATES.has(row.state)) return null;
  return { eventId: row.event_id, state: row.state as WithdrawalState, requestedAt: row.requested_at };
}

const known = <T>(rows: unknown, parse: (row: unknown) => T | null): T[] =>
  (Array.isArray(rows) ? rows : []).flatMap((row) => {
    const found = parse(row);
    return found === null ? [] : [found];
  });

/** The answer of `GET /v1/tasks/{task}/queue`, or null when it is malformed. */
export function taskQueue(body: unknown): TaskQueue | null {
  if (!isRecord(body) || !Array.isArray(body.prompts)) return null;
  return {
    prompts: known(body.prompts, queuedPrompt).sort((a, b) => a.scopeSeq - b.scopeSeq),
    withdrawals: known(body.withdrawals, withdrawal),
  };
}

const taskPath = (task: string): string => `/v1/tasks/${encodeURIComponent(task)}`;

export async function loadQueue(context: RequestsContext, task: string): Promise<TaskQueue> {
  const generation = context.store.generation;
  const found = taskQueue(await context.client.request('GET', `${taskPath(task)}/queue`));
  if (found === null) throw new HubError('unavailable', 200);
  if (context.store.generation === generation) putQueue(context.store, task, found);
  return found;
}

/** The queue as it is after a withdrawal the hub accepted (until the next read). */
function withdrawn(queue: TaskQueue, event: string, state: 'withdrawn' | 'withdrawing', at: string): TaskQueue {
  const prompts =
    state === 'withdrawn'
      ? queue.prompts.filter((prompt) => prompt.eventId !== event)
      : queue.prompts.map((prompt) =>
          prompt.eventId === event ? { ...prompt, state: 'withdrawing' as const } : prompt,
        );
  const rest = queue.withdrawals.filter((item) => item.eventId !== event);
  return { prompts, withdrawals: [{ eventId: event, state, requestedAt: at }, ...rest] };
}

/** Withdraws the waiting prompt `event` of `task` (module comment); returns the hub's answer. */
export async function withdrawPrompt(
  context: RequestsContext,
  task: string,
  event: string,
): Promise<'withdrawn' | 'withdrawing'> {
  const answer = await context.client.request(
    'POST',
    `${taskPath(task)}/prompts/${encodeURIComponent(event)}/withdraw`,
    { body: {} },
  );
  const state = isRecord(answer) ? answer.state : null;
  if (state !== 'withdrawn' && state !== 'withdrawing') throw new HubError('unavailable', 200);
  const queue = context.store.getState().queue.byTask[task];
  if (queue !== undefined) {
    putQueue(context.store, task, withdrawn(queue, event, state, new Date(context.now()).toISOString()));
  }
  await loadQueue(context, task).catch(() => undefined);
  return state;
}

const rawHeader = (entry: EventEntry): Readonly<Record<string, unknown>> =>
  isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};

/** Task ID → number of `prompt` events of a workgroup scope (by the unchecked header: only a trigger to read). */
const promptCounts = memoize((scope: ScopeEvents): ReadonlyMap<unknown, number> => {
  const counts = new Map<unknown, number>();
  for (const entry of scope.entries.values()) {
    const header = rawHeader(entry);
    if (header.kind === 'prompt') counts.set(header.task, (counts.get(header.task) ?? 0) + 1);
  }
  return counts;
});

/** Task ID → its session entries (a `session` frame replaces the entry, also when the row is unchanged). */
const sessionsByTask = memoize((sessions: TasksState['sessions']): ReadonlyMap<string, readonly SessionEntry[]> => {
  const found = new Map<string, SessionEntry[]>();
  for (const entry of Object.values(sessions)) {
    const list = found.get(entry.row.task_id) ?? [];
    list.push(entry);
    found.set(entry.row.task_id, list);
  }
  return found;
});

const NO_SESSIONS: readonly SessionEntry[] = [];

/** What a change of which reads the queue again: the task row, its sessions, the number of its prompt events. */
export interface QueueInputs {
  readonly row: TaskRow | undefined;
  readonly sessions: readonly SessionEntry[];
  readonly prompts: number;
}

export function queueInputs(state: StoreState, task: string): QueueInputs {
  const row = state.tasks.tasks[task];
  const scope = row === undefined ? undefined : state.events[row.wg_id];
  return {
    row,
    sessions: sessionsByTask(state.tasks.sessions).get(task) ?? NO_SESSIONS,
    prompts: scope === undefined ? 0 : (promptCounts(scope).get(task) ?? 0),
  };
}

/** Equal inputs: the same task row and session entries (by identity) and the same number of prompts. */
export function sameInputs(a: QueueInputs, b: QueueInputs): boolean {
  return (
    a.row === b.row &&
    a.prompts === b.prompts &&
    a.sessions.length === b.sessions.length &&
    a.sessions.every((entry, index) => entry === b.sessions[index])
  );
}

export interface QueueWatchOptions {
  readonly onError?: (code: string) => void;
  readonly debounceMs?: number;
  readonly pollMs?: number;
}

/** Starts watching the queue of `task` (module comment); the returned function stops it. */
export function watchQueue(context: RequestsContext, task: string, options: QueueWatchOptions = {}): () => void {
  let stopped = false;
  let running = false;
  let again = false;
  let pending: ReturnType<typeof setTimeout> | null = null;
  const read = async (): Promise<void> => {
    if (running) {
      again = true;
      return;
    }
    running = true;
    try {
      await loadQueue(context, task);
    } catch (error) {
      if (!stopped) options.onError?.(requestErrorCode(error));
    } finally {
      running = false;
      if (again && !stopped) {
        again = false;
        void read();
      }
    }
  };
  const later = (): void => {
    if (pending !== null) clearTimeout(pending);
    pending = setTimeout(() => {
      pending = null;
      void read();
    }, options.debounceMs ?? QUEUE_DEBOUNCE_MS);
  };
  const unsubscribe = subscribe(context.store, (state) => queueInputs(state, task), later, sameInputs);
  const poll = setInterval(() => {
    const waiting = context.store.getState().queue.byTask[task]?.prompts.length ?? 0;
    if (waiting > 0) void read();
  }, options.pollMs ?? QUEUE_POLL_MS);
  void read();
  return () => {
    stopped = true;
    unsubscribe();
    clearInterval(poll);
    if (pending !== null) clearTimeout(pending);
  };
}
