// The countdown of a session start (06-web.md section 4.4, ADR-009) as a pure function of the store and the time, so
// that it is tested with fake time and the panel only renders it:
//
// - `counting`: starts at `min(estimate_ms, 10 s)` – from the moment the `POST /v1/tasks` answer arrived for a task this
//   browser launched, from the moment the estimate appeared for a session that waited (`estimatedAt`), else from the
//   session's creation on the hub clock (hub clock offset from `hello.hub_time`); whole seconds, tenths below 3 s;
// - `delayed`: the countdown reached 0 before `working` – the current step and the live elapsed time;
// - `waiting`: no estimate (`scheduling`): the real reason (a waiting reason of ADR-075 section 7) and the elapsed time;
// - `ready`: `working` reached – "Ready in x s" (`first_working_at − task.created_at`, both hub clock);
// - `failed`: the session failed – its reason (catalog) and "Retry".
//
// The status steps follow the session's transitions (`requested` … `working`): a step is done when it or a later step
// was reached (runners may skip start-up steps), with the device-reported time (`at`) and the elapsed time since the
// task was created (`received_at − created_at`, hub clock); the step after the furthest reached one is current.
import { parseTime } from '../encoding/time';
import type { Launch, SessionEntry, TaskRow, Transition } from '../store/slices/tasks';

export const STEPS = [
  'requested',
  'vm_assigning',
  'vm_ready',
  'sandbox_ready',
  'agent_starting',
  'prompt_delivering',
  'working',
] as const;
export type Step = (typeof STEPS)[number];

/** The countdown never starts above ten seconds (KONZEPT §3, ADR-009). */
export const CAP_MS = 10_000;
/** Below this the digits show tenths. */
export const TENTHS_BELOW_MS = 3_000;
/** The screen-reader announcement repeats at this interval (06-web.md section 12.3). */
export const ANNOUNCE_EVERY_MS = 5_000;
/** Task states (`contracts/common.json` `task_state`), the filter of the task list. */
export const TASK_STATES = ['scheduling', 'starting', 'working', 'idle', 'failed', 'closed'] as const;
/** Reasons a waiting task names (03-api.md section 4.6.2). */
export const WAITING_REASONS = ['device_offline', 'capacity_exhausted'] as const;

/** Start-up states that report work in progress: reached, they are the current step (06-web.md section 4.4: "Starting
 *  the agent – 12.4 s" while the session is `agent_starting`); the others report a finished step. */
const IN_PROGRESS = new Set(['vm_assigning', 'agent_starting', 'prompt_delivering']);

/** States in which the session has been working at least once (the start is over). */
const AFTER_START = new Set(['working', 'idle', 'suspended', 'resuming', 'stopping', 'stopped']);

export type Phase = 'counting' | 'delayed' | 'waiting' | 'ready' | 'failed';
export type StepStatus = 'done' | 'current' | 'failed' | 'pending';

export interface StepView {
  readonly state: Step;
  readonly status: StepStatus;
  /** Device-reported time of the step (unix ms), null when the step was skipped or not reached. */
  readonly at: number | null;
  /** Hub receipt time minus task creation (ms), null like `at`. */
  readonly elapsedMs: number | null;
}

export interface CountdownView {
  readonly phase: Phase;
  /** The start value `min(estimate, 10 s)`; null without an estimate. */
  readonly totalMs: number | null;
  /** Remaining time (ms, ≥ 0); null without an estimate. */
  readonly remainingMs: number | null;
  /** Remaining share for the progress ring (1 → 0). */
  readonly progress: number;
  /** Elapsed time since the task was created (ms, ≥ 0). */
  readonly elapsedMs: number;
  readonly steps: readonly StepView[];
  /** The step in progress (`delayed`/`waiting`: shown with the elapsed time), null when ready. */
  readonly current: Step | null;
  /** `waiting`: the scheduler's reason; `failed`: the session failure reason. */
  readonly reason: string | null;
  /** `ready`: `first_working_at − created_at` (ms). */
  readonly readyInMs: number | null;
  /** The automatic device assignment before the task existed (ms), null if none happened. */
  readonly assignedMs: number | null;
}

export interface CountdownInput {
  readonly task: TaskRow;
  readonly session: SessionEntry | null;
  /** The launch of this task by this browser (used only for the session it names; the caller passes the launch of
   *  the session it follows). */
  readonly launch: Launch | null;
  readonly hubOffsetMs: number | null;
  /** Browser clock (unix ms). */
  readonly now: number;
}

const stepIndex = (state: string): number => (STEPS as readonly string[]).indexOf(state);

/** `{value, fractionDigits}` of the digits: whole seconds (rounded up), tenths below 3 s. */
export function digits(remainingMs: number): { readonly value: number; readonly fractionDigits: 0 | 1 } {
  const ms = Math.max(0, remainingMs);
  if (ms > TENTHS_BELOW_MS) return { value: Math.ceil(ms / 1000), fractionDigits: 0 };
  return { value: Math.ceil(ms / 100) / 10, fractionDigits: 1 };
}

function firstOf(transitions: readonly Transition[]): Map<string, Transition> {
  const first = new Map<string, Transition>();
  for (const item of transitions) if (!first.has(item.state)) first.set(item.state, item);
  return first;
}

/** The furthest start-up step reached (-1: none known). */
function furthest(session: SessionEntry | null, first: Map<string, Transition>): number {
  let index = session === null ? -1 : stepIndex(session.row.state);
  for (const state of first.keys()) index = Math.max(index, stepIndex(state));
  return index;
}

function workingAt(session: SessionEntry | null, first: Map<string, Transition>): number | null {
  if (session === null) return null;
  const at = session.row.first_working_at ?? first.get('working')?.received_at ?? null;
  if (at !== null) return parseTime(at) - parseTime(session.row.created_at);
  return AFTER_START.has(session.row.state) ? 0 : null;
}

/** The countdown of a task's (main) session at `now`. */
export function countdown(input: CountdownInput): CountdownView {
  const { task, session, now } = input;
  // the launch belongs to the session it names; before that session's row arrived it is all there is
  const own = input.launch;
  const launch = own !== null && (session === null || own.sessionId === session.row.session_id) ? own : null;
  // times count from the session's creation (the task's for its first session; a retry or follow-up starts anew)
  const created = parseTime(session?.row.created_at ?? task.created_at);
  const hubNow = now + (input.hubOffsetMs ?? 0);
  const elapsedMs = Math.max(
    0,
    input.hubOffsetMs === null && launch !== null ? now - launch.localAt : hubNow - created,
  );
  const first = firstOf(session?.transitions ?? []);
  const reachedIndex = furthest(session, first);
  const readyInMs = workingAt(session, first);
  const failed = session?.row.state === 'failed' || (session === null && task.state === 'failed');
  const ready = readyInMs !== null && !failed;
  const estimate = session?.row.estimate_ms ?? launch?.estimateMs ?? null;
  const totalMs = estimate === null ? null : Math.min(estimate, CAP_MS);
  let remainingMs: number | null = null;
  if (totalMs !== null) {
    const anchor =
      (launch !== null && launch.estimateMs !== null ? launch.localAt : null) ??
      // an estimate without a launch of this session comes from the session row
      session!.estimatedAt ??
      parseTime(session!.row.created_at) - (input.hubOffsetMs ?? 0);
    remainingMs = Math.max(0, totalMs - (now - anchor));
  }
  let phase: Phase;
  if (failed) phase = 'failed';
  else if (ready) phase = 'ready';
  else if (totalMs === null) phase = 'waiting';
  else phase = remainingMs! > 0 ? 'counting' : 'delayed';
  const inProgress = reachedIndex >= 0 && IN_PROGRESS.has(STEPS[reachedIndex]!);
  const next = inProgress ? reachedIndex : reachedIndex + 1;
  const currentIndex = phase === 'ready' ? -1 : Math.min(next, STEPS.length - 1);
  const steps = STEPS.map((state, index): StepView => {
    const reached = first.get(state);
    let status: StepStatus = 'pending';
    if (phase === 'ready' || index < next) status = 'done';
    else if (index === currentIndex) status = phase === 'failed' ? 'failed' : 'current';
    return {
      state,
      status,
      at: reached === undefined ? null : parseTime(reached.at),
      elapsedMs: reached === undefined ? null : parseTime(reached.received_at) - created,
    };
  });
  let reason: string | null = null;
  if (phase === 'failed') reason = session?.row.state_reason ?? task.state_reason;
  if (phase === 'waiting') reason = task.state === 'scheduling' ? task.state_reason : null;
  return {
    phase,
    totalMs,
    remainingMs,
    progress: totalMs === null || totalMs === 0 ? 0 : remainingMs! / totalMs,
    elapsedMs,
    steps,
    current: currentIndex < 0 ? null : STEPS[currentIndex]!,
    reason,
    readyInMs: ready ? readyInMs : null,
    assignedMs: launch?.assignedMs ?? null,
  };
}

/**
 * Whether the start is over (`working` reached or `failed`): such a panel needs no clock. Independent of the time, so
 * the panel can decide before it reads the clock.
 */
export function settled(task: TaskRow, session: SessionEntry | null): boolean {
  if (session === null) return task.state === 'failed';
  const first = firstOf(session.transitions);
  return session.row.state === 'failed' || workingAt(session, first) !== null;
}

/**
 * What the live region says (06-web.md section 12.3): the phase, the current step and, while counting, the remaining
 * seconds rounded up to `ANNOUNCE_EVERY_MS`, else the elapsed seconds rounded down to it – so the text changes on
 * every step change and every 5 s, and only then.
 */
export function announcement(view: CountdownView): {
  readonly phase: Phase;
  readonly step: Step | null;
  readonly seconds: number;
} {
  const every = ANNOUNCE_EVERY_MS;
  const seconds =
    view.phase === 'counting'
      ? (Math.ceil(view.remainingMs! / every) * every) / 1000
      : (Math.floor(view.elapsedMs / every) * every) / 1000;
  return { phase: view.phase, step: view.current, seconds };
}

/** The countdown while the send is still running (before the task exists): from 10 s since the send started. */
export function preparing(startedAt: number, now: number): { readonly remainingMs: number; readonly progress: number } {
  const remainingMs = Math.max(0, CAP_MS - (now - startedAt));
  return { remainingMs, progress: remainingMs / CAP_MS };
}
