// Slice `queue` (06-web.md section 5.2, ADR-083 section 3): per task the prompts not yet delivered to a live main
// session and the task's withdrawals with their outcome, as `GET /v1/tasks/{task}/queue` last reported them
// (`core/chat/queue.ts` reads and parses the answer). The chat shows the queue above the composer and marks withdrawn
// prompts in the timeline.
import { memoize } from '../selectors';
import type { Slice, Store, StoreState } from '../store';

export type QueueState = 'waiting' | 'waiting_for_device' | 'withdrawing';
export type WithdrawalState = 'withdrawn' | 'withdrawing' | 'too_late';

/** `queue_prompt` of the contracts. */
export interface QueuedPrompt {
  readonly eventId: string;
  readonly authorId: string;
  /** The hub received the prompt from the author's agent. */
  readonly viaAgent: boolean;
  readonly scopeSeq: number;
  readonly receivedAt: string;
  readonly sessionId: string;
  readonly deviceId: string | null;
  readonly state: QueueState;
}

export interface Withdrawal {
  readonly eventId: string;
  readonly state: WithdrawalState;
  readonly requestedAt: string;
}

export interface TaskQueue {
  /** In event order. */
  readonly prompts: readonly QueuedPrompt[];
  /** Newest first. */
  readonly withdrawals: readonly Withdrawal[];
}

export interface QueueSliceState {
  readonly byTask: Readonly<Record<string, TaskQueue>>;
}

declare module '../store' {
  interface StoreState {
    queue: QueueSliceState;
  }
}

export const slice: Slice<'queue'> = { name: 'queue', initial: () => ({ byTask: {} }) };

export function putQueue(store: Store, task: string, queue: TaskQueue): void {
  store.update('queue', (state) => ({ ...state, byTask: { ...state.byTask, [task]: queue } }));
}

/** Prompt event → the outcome of its newest withdrawal, over every loaded queue (built once per slice state). */
const withdrawalIndex = memoize((state: QueueSliceState): ReadonlyMap<string, WithdrawalState> => {
  const index = new Map<string, WithdrawalState>();
  for (const queue of Object.values(state.byTask)) {
    for (const item of queue.withdrawals) if (!index.has(item.eventId)) index.set(item.eventId, item.state);
  }
  return index;
});

/** The outcome of the newest withdrawal of prompt `event` in any loaded queue, else null. */
export const selectWithdrawal =
  (event: string) =>
  (state: StoreState): WithdrawalState | null =>
    withdrawalIndex(state.queue).get(event) ?? null;
