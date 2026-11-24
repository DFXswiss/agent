// The work VM events of a session (13-work-vm.md sections 8 and 10, ADR-059 decisions 8-10):
//
// - `parseWorkvmRun` / `parseWorkvmState`: the decrypted body checked against `contracts/events/objects.json`
//   (`body_workvm_run`, `body_workvm_state`: every field required, no other field); anything else is null and the
//   chat shows a neutral row instead of content;
// - `latestRuns`: a job is written when it starts and again when it is final; the timeline keeps only the latest
//   item per `job_id`, so a running item is replaced by the final one;
// - `workvmRuntime`: the runtime of a set of work VMs (KONZEPT idea 46): `runtime_ms` of a VM's `deleted` event, or
//   `now − started_at` while it is created and not deleted yet;
// - `selectWorkvmStates`: the valid `workvm.state` bodies of a task and of one of its sessions (every VM the session
//   wrote a state event of or ran a job in, with all events of that VM in the task), memoized on the scope's events;
// - `selectWorkvmRuns`: the latest valid `workvm.run` body per job of one session, newest start first (the run list
//   of the session panel; the logs are the stored output tails, KONZEPT idea 27);
// - `selectWorkvmRequests` and `workvmPanel`: the state of the session's work VM for the session panel – `starting`
//   while a `workvm.request` operation of the session is open and no VM of the session runs (with the countdown to
//   the 10 s target of KONZEPT §3), `running`, `deleted` (the last VM, with its reason) or `none`.
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import { FINAL_STATES, type OperationRow } from '../store/slices/operations';
import type { ScopeEvents } from '../store/slices/events';
import type { StoreState } from '../store/store';

export const RUN_KIND = 'workvm.run';
export const STATE_KIND = 'workvm.state';

export const RUN_STATES = ['running', 'exited', 'killed', 'timed_out', 'lost'] as const;
export type RunState = (typeof RUN_STATES)[number];

export interface WorkvmRun {
  readonly workvm_id: string;
  readonly job_id: string;
  readonly kind: 'exec' | 'browse';
  readonly command: string;
  readonly state: RunState;
  readonly exit_code: number | null;
  readonly started_at: string;
  readonly ended_at: string | null;
  readonly duration_ms: number;
  readonly output: string;
  readonly output_bytes: number;
}

export interface WorkvmState {
  readonly workvm_id: string;
  readonly state: 'created' | 'deleted';
  readonly scope: 'task' | 'session';
  readonly size: 'standard' | 'large' | 'xlarge';
  readonly cpus: number;
  readonly memory_mib: number;
  readonly disk_gib: number;
  readonly implicit: boolean;
  readonly reason: 'released' | 'idle' | 'session_ended' | 'task_closed' | 'failed' | 'restart' | null;
  readonly started_at: string;
  readonly ended_at: string | null;
  readonly runtime_ms: number;
}

type Check = (value: unknown) => boolean;

const pattern =
  (re: RegExp): Check =>
  (value) =>
    typeof value === 'string' && re.test(value);
const oneOf =
  (...values: readonly unknown[]): Check =>
  (value) =>
    values.includes(value);
const integer =
  (min: number, max: number): Check =>
  (value) =>
    Number.isSafeInteger(value) && (value as number) >= min && (value as number) <= max;
/** JSON Schema `maxLength` counts code points. */
const text =
  (max: number): Check =>
  (value) =>
    typeof value === 'string' && [...value].length <= max;
const nullable =
  (check: Check): Check =>
  (value) =>
    value === null || check(value);
const time: Check = (value) => {
  try {
    parseTime(value);
    return true;
  } catch {
    return false;
  }
};
const count = integer(0, Number.MAX_SAFE_INTEGER);
const workvmId = pattern(/^wvm-[0-9a-f]{8}$/);

const RUN_SHAPE: Readonly<Record<keyof WorkvmRun, Check>> = {
  workvm_id: workvmId,
  job_id: pattern(/^j-[0-9a-f]{12}$/),
  kind: oneOf('exec', 'browse'),
  command: text(4096),
  state: oneOf(...RUN_STATES),
  exit_code: nullable(integer(0, 255)),
  started_at: time,
  ended_at: nullable(time),
  duration_ms: count,
  output: text(65536),
  output_bytes: count,
};

const STATE_SHAPE: Readonly<Record<keyof WorkvmState, Check>> = {
  workvm_id: workvmId,
  state: oneOf('created', 'deleted'),
  scope: oneOf('task', 'session'),
  size: oneOf('standard', 'large', 'xlarge'),
  cpus: integer(1, 64),
  memory_mib: integer(512, 262144),
  disk_gib: integer(1, 4096),
  implicit: oneOf(true, false),
  reason: nullable(oneOf('released', 'idle', 'session_ended', 'task_closed', 'failed', 'restart')),
  started_at: time,
  ended_at: nullable(time),
  runtime_ms: count,
};

/** Every field of `shape` present and valid, no other field. */
function matches(body: unknown, shape: Readonly<Record<string, Check>>): boolean {
  if (!isRecord(body)) return false;
  const keys = Object.keys(body);
  return (
    keys.length === Object.keys(shape).length &&
    keys.every((key) => Object.hasOwn(shape, key) && shape[key]!(body[key]))
  );
}

/** A `workvm.run` body, or null when it does not follow the contract. */
export const parseWorkvmRun = (body: unknown): WorkvmRun | null =>
  matches(body, RUN_SHAPE) ? (body as unknown as WorkvmRun) : null;

/** A `workvm.state` body, or null when it does not follow the contract. */
export const parseWorkvmState = (body: unknown): WorkvmState | null =>
  matches(body, STATE_SHAPE) ? (body as unknown as WorkvmState) : null;

/** Whether the output is only the tail of the job's output (`output_bytes` counts the whole output in UTF-8). */
export const outputTruncated = (run: WorkvmRun): boolean =>
  new TextEncoder().encode(run.output).length < run.output_bytes;

/**
 * `items` (in timeline order) without the `workvm.run` items that a later item of the same `job_id` replaces;
 * items with an invalid body are kept (the chat shows them as neutral rows).
 */
export function latestRuns<T extends { readonly kind: string; readonly body: unknown }>(items: readonly T[]): T[] {
  const seen = new Set<string>();
  const kept: T[] = [];
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index]!;
    const job = item.kind === RUN_KIND ? parseWorkvmRun(item.body)?.job_id : undefined;
    if (job !== undefined) {
      if (seen.has(job)) continue;
      seen.add(job);
    }
    kept.push(item);
  }
  return kept.reverse();
}

export interface Runtime {
  /** Summed runtime in ms. */
  readonly ms: number;
  /** Number of distinct work VMs. */
  readonly vms: number;
  /** Whether one of them is still created (its runtime grows with `now`). */
  readonly running: boolean;
}

/**
 * The runtime of the work VMs of `states`: per `workvm_id` the `runtime_ms` of its (last) `deleted` event, else
 * `now − started_at` of its `created` event (never negative).
 */
export function workvmRuntime(states: readonly WorkvmState[], now: number): Runtime {
  const byVm = new Map<string, WorkvmState>();
  for (const state of states) {
    const known = byVm.get(state.workvm_id);
    if (known === undefined || state.state === 'deleted') byVm.set(state.workvm_id, state);
  }
  let ms = 0;
  let running = false;
  for (const state of byVm.values()) {
    if (state.state === 'deleted') {
      ms += state.runtime_ms;
    } else {
      running = true;
      ms += Math.max(0, now - parseTime(state.started_at));
    }
  }
  return { ms, vms: byVm.size, running };
}

const UNITS: readonly [string, number][] = [
  ['h', 3_600_000],
  ['min', 60_000],
  ['s', 1000],
];

/** `2 h 5 min 3 s`, `4 min`, `0 s`: the non-zero units of the whole seconds (hours are not folded into days). */
export function formatRuntime(ms: number): string {
  let rest = Math.floor(Math.max(0, ms) / 1000) * 1000;
  const parts: string[] = [];
  for (const [symbol, size] of UNITS) {
    const amount = Math.floor(rest / size);
    rest -= amount * size;
    if (amount > 0) parts.push(`${amount} ${symbol}`);
  }
  return parts.length === 0 ? '0 s' : parts.join(' ');
}

export interface WorkvmStates {
  readonly task: readonly WorkvmState[];
  readonly session: readonly WorkvmState[];
}

function collectStates(scope: ScopeEvents | undefined, task: string, session: string): WorkvmStates {
  const all: { session: unknown; body: WorkvmState }[] = [];
  // the VMs of the session: those it wrote a state event of, and those it ran a job in (the task VM another session
  // of the task obtained)
  const own = new Set<string>();
  for (const entry of scope?.entries.values() ?? []) {
    if (entry.status !== 'verified' || entry.header!.task !== task) continue;
    if (entry.header!.kind === RUN_KIND) {
      const run = entry.header!.session === session ? parseWorkvmRun(entry.body) : null;
      if (run !== null) own.add(run.workvm_id);
      continue;
    }
    if (entry.header!.kind !== STATE_KIND) continue;
    const body = parseWorkvmState(entry.body);
    if (body === null) continue;
    all.push({ session: entry.header!.session, body });
    if (entry.header!.session === session) own.add(body.workvm_id);
  }
  return {
    task: all.map((state) => state.body),
    session: all.filter((state) => own.has(state.body.workvm_id)).map((state) => state.body),
  };
}

/** A selector of the valid `workvm.state` bodies of `task` and of `session`, memoized on the scope's events. */
export function selectWorkvmStates(wg: string, task: string, session: string): (state: StoreState) => WorkvmStates {
  let last: { scope: unknown; states: WorkvmStates } | null = null;
  return (state) => {
    const scope = state.events[wg];
    if (last !== null && last.scope === scope) return last.states;
    last = { scope, states: collectStates(scope, task, session) };
    return last.states;
  };
}

/** Newest start first; equal starts keep the timeline order. */
function newestFirst(runs: WorkvmRun[]): WorkvmRun[] {
  return runs
    .map((run, index) => ({ run, index, at: parseTime(run.started_at) }))
    .sort((a, b) => b.at - a.at || a.index - b.index)
    .map((entry) => entry.run);
}

function collectRuns(scope: ScopeEvents | undefined, task: string, session: string): readonly WorkvmRun[] {
  const latest = new Map<string, WorkvmRun>();
  const entries = [...(scope?.entries.values() ?? [])].sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const entry of entries) {
    if (entry.status !== 'verified' || entry.header!.kind !== RUN_KIND) continue;
    if (entry.header!.task !== task || entry.header!.session !== session) continue;
    const body = parseWorkvmRun(entry.body);
    if (body === null) continue;
    latest.delete(body.job_id);
    latest.set(body.job_id, body);
  }
  return newestFirst([...latest.values()]);
}

/** A selector of the latest valid `workvm.run` body per job of `session`, newest start first (memoized). */
export function selectWorkvmRuns(
  wg: string,
  task: string,
  session: string,
): (state: StoreState) => readonly WorkvmRun[] {
  let last: { scope: unknown; runs: readonly WorkvmRun[] } | null = null;
  return (state) => {
    const scope = state.events[wg];
    if (last !== null && last.scope === scope) return last.runs;
    last = { scope, runs: collectRuns(scope, task, session) };
    return last.runs;
  };
}

export const REQUEST_TYPE = 'workvm.request';

/** A selector of the `workvm.request` operations of `session` (memoized on the operation rows). */
export function selectWorkvmRequests(session: string): (state: StoreState) => readonly OperationRow[] {
  let last: { rows: unknown; requests: readonly OperationRow[] } | null = null;
  return (state) => {
    const rows = state.operations.rows;
    if (last !== null && last.rows === rows) return last.requests;
    const requests = Object.values(rows).filter((row) => row.type === REQUEST_TYPE && row.session_id === session);
    last = { rows, requests };
    return last.requests;
  };
}

/** The ready target of a work VM (KONZEPT §3: under ten seconds). */
export const START_TARGET_MS = 10_000;

export type PanelPhase = 'none' | 'starting' | 'running' | 'deleted';

export interface WorkvmPanel {
  readonly phase: PanelPhase;
  /** The running VM, else the last VM of the session (by start), else null. */
  readonly vm: WorkvmState | null;
  /** `starting`: time since the oldest open request. */
  readonly elapsedMs: number;
  /** `starting`: time left to the 10 s target (never negative). */
  readonly remainingMs: number;
  /** `starting`: the target has passed. */
  readonly overdue: boolean;
}

/**
 * The state of the session's work VM: `states` are the session's `workvm.state` bodies (`selectWorkvmStates`),
 * `requests` its `workvm.request` operations, `now` the browser time.
 */
export function workvmPanel(
  states: readonly WorkvmState[],
  requests: readonly OperationRow[],
  now: number,
): WorkvmPanel {
  const byVm = new Map<string, WorkvmState>();
  for (const state of states) {
    const known = byVm.get(state.workvm_id);
    if (known === undefined || state.state === 'deleted') byVm.set(state.workvm_id, state);
  }
  let last: WorkvmState | null = null;
  for (const state of byVm.values()) {
    if (last === null || parseTime(state.started_at) >= parseTime(last.started_at)) last = state;
  }
  const running = [...byVm.values()].find((state) => state.state === 'created') ?? null;
  const idle = { elapsedMs: 0, remainingMs: 0, overdue: false };
  if (running !== null) return { phase: 'running', vm: running, ...idle };
  const open = requests.filter((row) => !FINAL_STATES.has(row.state)).map((row) => parseTime(row.created_at));
  if (open.length > 0) {
    const elapsedMs = Math.max(0, now - Math.min(...open));
    return {
      phase: 'starting',
      vm: last,
      elapsedMs,
      remainingMs: Math.max(0, START_TARGET_MS - elapsedMs),
      overdue: elapsedMs > START_TARGET_MS,
    };
  }
  return { phase: last === null ? 'none' : 'deleted', vm: last, ...idle };
}
