// Slice `jobs`: data of the "Sync" indicator (06-web.md section 11): per queue (an org ID, or `platform`) the running
// job, the number of waiting jobs and the outcome of the last run.
import type { Slice, Store } from '../store';

export interface JobOutcome {
  readonly name: string;
  readonly ok: boolean;
  readonly code: string | null;
  /** Unix ms of the end of the run. */
  readonly at: number;
}

export interface QueueStatus {
  readonly running: string | null;
  readonly waiting: number;
  readonly last: JobOutcome | null;
}

export type JobsState = Readonly<Record<string, QueueStatus>>;

declare module '../store' {
  interface StoreState {
    jobs: JobsState;
  }
}

export const slice: Slice<'jobs'> = { name: 'jobs', initial: () => ({}) };

const IDLE: QueueStatus = { running: null, waiting: 0, last: null };

export function setQueue(store: Store, queue: string, patch: Partial<QueueStatus>): void {
  store.update('jobs', (jobs) => ({ ...jobs, [queue]: { ...(jobs[queue] ?? IDLE), ...patch } }));
}

/** True while any queue runs or waits (the indicator spins). */
export function syncing(jobs: JobsState): boolean {
  return Object.values(jobs).some((queue) => queue.running !== null || queue.waiting > 0);
}
