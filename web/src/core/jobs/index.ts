// Background jobs (06-web.md section 11) and their registration point (ADR-027 section 4): every file
// `src/core/jobs/<name>.ts` exports `job` (name = file name, an order number, the triggers it reacts to and `run`).
// Packages add files and never edit this one. The runner runs jobs while signed in and unlocked, serialized per org
// (one queue per org), coalesces a job that is already waiting for the same org, skips orgs in the blocking error
// state and publishes the "Sync" indicator data in the `jobs` slice. A job that opts in with `retry` and fails with a
// transient hub error (`HubError` `unavailable` – also an unreachable hub – or `rate_limited`) makes the runner fire
// the same trigger for the org again with backoff (1 s → 30 s, jitter, as the live channel), until a run of the job
// succeeds, the org is blocked, a sign-out or `stop()`: a network failure at sign-in must not leave an org "being
// checked" (KOR-05).
import { CryptoError } from '../crypto/errors';
import type { SignedClient } from '../net/signedFetch';
import { HubError } from '../net/signedFetch';
import { backoff, SYSTEM_TIMERS, type Timers } from '../net/sse';
import type { Persist } from '../persist';
import { baseName, collect, isNamed, type ModuleMap, ordered } from '../registry';
import { setQueue } from '../store/slices/jobs';
import { orgEntry } from '../store/slices/orgs';
import type { Store } from '../store/store';

/** What starts a job: sign-in/unlock, an SSE `log` or `wraps` frame, an event with `lh` beyond the verified head. */
export type Trigger = 'login' | 'log' | 'lh' | 'wraps' | 'interval';

export interface JobContext {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  readonly persist: Pick<Persist, 'loadPinned' | 'savePinned' | 'loadPlatformHead' | 'savePlatformHead'>;
  /** The platform root key compiled into the build (b64u). */
  readonly rootKey: string;
  readonly now: () => number;
  /**
   * Called after a job changed what the org's events can be checked against (new log, new keys). Jobs never await
   * it: the listener (the reader's retry) may trigger jobs of the same org, which wait for the running one.
   */
  readonly changed?: (org: string) => unknown;
}

export interface JobRun {
  readonly org: string;
  readonly trigger: Trigger;
  /** `store.generation` when the run started; a run whose generation is gone (sign-out) writes nothing. */
  readonly generation: number;
}

/** Whether a run may still write (no sign-out since it started). */
export function current(context: JobContext, run: JobRun): boolean {
  return context.store.generation === run.generation;
}

export interface JobRegistration {
  readonly name: string;
  /** Jobs of one trigger run in this order (log verification before wrap verification). */
  readonly order: number;
  readonly triggers: readonly Trigger[];
  readonly run: (context: JobContext, run: JobRun) => Promise<void>;
  /** A run that failed with a transient error (`TRANSIENT_CODES`) is repeated with backoff (module comment). */
  readonly retry?: boolean;
}

/** Codes of a `HubError` after which a repeated run can succeed (other failures are bugs or refusals). */
export const TRANSIENT_CODES: ReadonlySet<string> = new Set(['unavailable', 'rate_limited']);

export interface RunnerOptions {
  readonly timers?: Timers;
  readonly random?: () => number;
}

const isJob = (value: unknown): value is JobRegistration =>
  isNamed(value) &&
  Number.isFinite((value as { order?: unknown }).order) &&
  Array.isArray((value as { triggers?: unknown }).triggers) &&
  typeof (value as { run?: unknown }).run === 'function';

const jobModules: ModuleMap = import.meta.glob(['./*.ts', '!./index.ts'], { eager: true });

/** Registered jobs, sorted by order and name; a job's name must equal its file name. */
export function backgroundJobs(modules: ModuleMap = jobModules): JobRegistration[] {
  const jobs = collect(modules, 'job', isJob).map(({ path, value }) => {
    if (value.name !== baseName(path, '.ts')) throw new Error(`${path}: job name "${value.name}" must match the file`);
    return value;
  });
  return ordered(jobs.map((job) => ({ ...job, id: job.name }))).map(({ id: _id, ...job }) => job);
}

/** The code of a failed run: a protocol code, otherwise `unavailable`. */
export function failureCode(error: unknown): string {
  if (error instanceof CryptoError || error instanceof HubError) return error.code;
  return 'unavailable';
}

interface Queue {
  tail: Promise<unknown>;
  /** Waiting (not started) runs by job name. */
  readonly waiting: Map<string, Promise<boolean>>;
}

interface Retry {
  /** Failed retrying runs in a row (the backoff attempt). */
  attempt: number;
  /** The pending timer, null while none is armed. */
  timer: unknown;
}

export class JobRunner {
  readonly #context: JobContext;
  readonly #jobs: readonly JobRegistration[];
  readonly #queues = new Map<string, Queue>();
  readonly #timers: Timers;
  readonly #random: () => number;
  /** Per `<trigger> <org>`: the backoff state of a failed retrying job. */
  readonly #retries = new Map<string, Retry>();
  #stopped = false;

  constructor(context: JobContext, jobs: readonly JobRegistration[] = backgroundJobs(), options: RunnerOptions = {}) {
    this.#context = context;
    this.#jobs = jobs;
    this.#timers = options.timers ?? SYSTEM_TIMERS;
    this.#random = options.random ?? Math.random;
  }

  /**
   * Runs every job that reacts to `trigger` for `org`. Resolves with true when all of them completed, false if one
   * failed or was skipped (blocked org, stopped runner); the outcomes land in the `jobs` slice.
   */
  async trigger(trigger: Trigger, org: string): Promise<boolean> {
    if (this.#stopped) return false;
    const run = { org, trigger, generation: this.#context.store.generation };
    const jobs = this.#jobs.filter((job) => job.triggers.includes(trigger));
    const results = await Promise.all(jobs.map((job) => this.#enqueue(job, run)));
    return results.every(Boolean);
  }

  /** Sign-out: nothing further runs (pending retries are cancelled); a run in progress finishes without writing. */
  stop(): void {
    this.#stopped = true;
    for (const retry of this.#retries.values()) if (retry.timer !== null) this.#timers.clearTimeout(retry.timer);
    this.#retries.clear();
  }

  /** After a run of a retrying job: reset the backoff on success, arm the next attempt after a transient failure. */
  #settleRetry(job: JobRegistration, run: JobRun, code: string | null, transient: boolean): void {
    if (job.retry !== true || this.#stopped) return;
    const key = `${run.trigger} ${run.org}`;
    if (code === null) {
      const retry = this.#retries.get(key);
      if (retry?.timer === null) this.#retries.delete(key);
      return;
    }
    if (!transient) return;
    const retry = this.#retries.get(key) ?? { attempt: 0, timer: null };
    this.#retries.set(key, retry);
    if (retry.timer !== null) return;
    retry.attempt += 1;
    retry.timer = this.#timers.setTimeout(
      () => {
        retry.timer = null;
        if (current(this.#context, run)) void this.trigger(run.trigger, run.org);
      },
      backoff(retry.attempt, this.#random()),
    );
  }

  #queue(org: string): Queue {
    let queue = this.#queues.get(org);
    if (queue === undefined) {
      queue = { tail: Promise.resolve(), waiting: new Map() };
      this.#queues.set(org, queue);
    }
    return queue;
  }

  #enqueue(job: JobRegistration, run: JobRun): Promise<boolean> {
    const queue = this.#queue(run.org);
    const waiting = queue.waiting.get(job.name);
    if (waiting !== undefined) return waiting;
    const promise = queue.tail.then(() => this.#run(job, run, queue));
    queue.waiting.set(job.name, promise);
    queue.tail = promise;
    setQueue(this.#context.store, run.org, { waiting: queue.waiting.size });
    return promise;
  }

  async #run(job: JobRegistration, run: JobRun, queue: Queue): Promise<boolean> {
    queue.waiting.delete(job.name);
    const store = this.#context.store;
    if (this.#stopped || !current(this.#context, run)) return false;
    if (orgEntry(store.getState().orgs, run.org).blocked !== null) {
      setQueue(store, run.org, { waiting: queue.waiting.size });
      return false;
    }
    setQueue(store, run.org, { running: job.name, waiting: queue.waiting.size });
    let code: string | null = null;
    let transient = false;
    try {
      await job.run(this.#context, run);
    } catch (error) {
      code = failureCode(error);
      transient = error instanceof HubError && TRANSIENT_CODES.has(error.code);
    }
    const last = { name: job.name, ok: code === null, code, at: this.#context.now() };
    if (!current(this.#context, run)) return code === null;
    setQueue(store, run.org, { running: null, last });
    this.#settleRetry(job, run, code, transient);
    return code === null;
  }
}
