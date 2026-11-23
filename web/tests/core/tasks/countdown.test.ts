// U-WEB-ui-tasks (countdown, 06-web.md section 4.4) with fake time: on time, delayed, waiting (offline, no slot), a
// session that got its estimate later, ready, failed, skipped steps, the digits and the announcements.
import { describe, expect, it } from 'vitest';
import { parseTime } from '../../../src/core/encoding/time';
import type { Launch, SessionEntry, TaskRow, Transition } from '../../../src/core/store/slices/tasks';
import { announcement, CAP_MS, countdown, digits, preparing, settled, STEPS } from '../../../src/core/tasks/countdown';
import { sessionRow, taskRow } from './world';

const T = { org: 'o', wg: 'w', me: { id: 'u' } } as never;
const CREATED = parseTime('2026-11-21T12:00:00.000Z');
const at = (ms: number) => new Date(CREATED + ms).toISOString();

const task = (overrides: Record<string, unknown> = {}) => taskRow(T, overrides) as TaskRow;
function session(
  overrides: Record<string, unknown> = {},
  transitions: Transition[] = [],
  estimatedAt: number | null = null,
) {
  return { row: sessionRow(T, overrides), transitions, estimatedAt } as unknown as SessionEntry;
}
const step = (seq: number, state: string, ms: number, reason: string | null = null): Transition => ({
  seq,
  state,
  reason,
  at: at(ms - 40),
  received_at: at(ms),
});
const launch = (overrides: Partial<Launch> = {}): Launch => ({
  sessionId: sessionRow(T).session_id,
  localAt: CREATED + 100,
  estimateMs: 3000,
  assignedMs: null,
  ...overrides,
});

describe('digits', () => {
  it('shows whole seconds rounded up, tenths below 3 s, never below 0', () => {
    expect(digits(9001)).toEqual({ value: 10, fractionDigits: 0 });
    expect(digits(3001)).toEqual({ value: 4, fractionDigits: 0 });
    expect(digits(3000)).toEqual({ value: 3, fractionDigits: 1 });
    expect(digits(2401)).toEqual({ value: 2.5, fractionDigits: 1 });
    expect(digits(-5)).toEqual({ value: 0, fractionDigits: 1 });
  });
});

describe('countdown', () => {
  it('on time: counts from min(estimate, 10 s) since the launch, with the steps reached so far', () => {
    const entry = session({ estimate_ms: 30_000 }, [step(1, 'requested', 0), step(2, 'vm_assigning', 400)]);
    const view = countdown({
      task: task(),
      session: entry,
      launch: launch({ estimateMs: 30_000 }),
      hubOffsetMs: null,
      now: CREATED + 100 + 2500,
    });
    expect(view.phase).toBe('counting');
    expect(view.totalMs).toBe(CAP_MS);
    expect(view.remainingMs).toBe(7500);
    expect(view.progress).toBe(0.75);
    expect(view.elapsedMs).toBe(2500);
    // `vm_assigning` reports work in progress: reached, it is the current step
    expect(view.current).toBe('vm_assigning');
    expect(view.steps.map((s) => s.status)).toEqual([
      'done',
      'current',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
    ]);
    expect(view.steps[1]).toEqual({ state: 'vm_assigning', status: 'current', at: CREATED + 360, elapsedMs: 400 });
    expect(view.steps[2]!.at).toBeNull();
    expect(view.reason).toBeNull();
    expect(view.readyInMs).toBeNull();
  });

  it('uses the hub clock offset for elapsed times and the session creation without a launch', () => {
    const view = countdown({
      task: task(),
      session: session({ estimate_ms: 8000 }),
      launch: null,
      hubOffsetMs: 2000,
      now: CREATED + 1000,
    });
    expect(view.elapsedMs).toBe(3000);
    expect(view.remainingMs).toBe(5000);
    const own = countdown({ task: task(), session: session(), launch: launch(), hubOffsetMs: 500, now: CREATED + 600 });
    expect(own.elapsedMs).toBe(1100);
    expect(own.remainingMs).toBe(2500);
    const early = countdown({ task: task(), session: null, launch: null, hubOffsetMs: null, now: CREATED - 50 });
    expect(early.elapsedMs).toBe(0);
  });

  it('ignores a launch of another session and takes its estimate when the row has none', () => {
    const other = countdown({
      task: task(),
      session: session({ estimate_ms: 8000 }),
      launch: launch({ sessionId: 'other', estimateMs: 1000 }),
      hubOffsetMs: 0,
      now: CREATED + 1000,
    });
    expect(other.remainingMs).toBe(7000);
    expect(other.assignedMs).toBeNull();
    const fallback = countdown({
      task: task(),
      session: session({ estimate_ms: null }),
      launch: launch({ assignedMs: 420 }),
      hubOffsetMs: null,
      now: CREATED + 100,
    });
    expect(fallback.totalMs).toBe(3000);
    expect(fallback.assignedMs).toBe(420);
    const before = countdown({ task: task(), session: null, launch: launch(), hubOffsetMs: null, now: CREATED + 600 });
    expect(before).toMatchObject({ phase: 'counting', remainingMs: 2500, current: 'requested' });
    const none = countdown({ task: task(), session: null, launch: null, hubOffsetMs: null, now: CREATED });
    expect(none.phase).toBe('waiting');
  });

  it('delayed: after 0 before working, with the current step and the elapsed time', () => {
    const view = countdown({
      task: task(),
      session: session({ estimate_ms: 2500, state: 'agent_starting' }, [step(1, 'requested', 0)]),
      launch: launch({ estimateMs: 2500 }),
      hubOffsetMs: null,
      now: CREATED + 100 + 12_400,
    });
    expect(view.phase).toBe('delayed');
    expect(view.remainingMs).toBe(0);
    expect(view.progress).toBe(0);
    expect(view.current).toBe('agent_starting');
    expect(view.elapsedMs).toBe(12_400);
    expect(view.steps.slice(0, 4).every((s) => s.status === 'done')).toBe(true);
    expect(view.steps[4]!.status).toBe('current');
  });

  it('waiting: no estimate, the reason of a scheduling task, the elapsed time', () => {
    const offline = countdown({
      task: task({ state: 'scheduling', state_reason: 'device_offline' }),
      session: session({ estimate_ms: null, device_id: null }),
      launch: launch({ estimateMs: null }),
      hubOffsetMs: 0,
      now: CREATED + 4200,
    });
    expect(offline).toMatchObject({ phase: 'waiting', reason: 'device_offline', totalMs: null, remainingMs: null });
    expect(offline.progress).toBe(0);
    expect(offline.elapsedMs).toBe(4200);
    const other = countdown({
      task: task({ state: 'starting' }),
      session: session({ estimate_ms: null }),
      launch: null,
      hubOffsetMs: null,
      now: CREATED,
    });
    expect(other.reason).toBeNull();
  });

  it('starts at the moment a waiting session got its estimate', () => {
    const view = countdown({
      task: task(),
      session: session({ estimate_ms: 3000 }, [], CREATED + 60_000),
      launch: launch({ estimateMs: null }),
      hubOffsetMs: 0,
      now: CREATED + 61_000,
    });
    expect(view.phase).toBe('counting');
    expect(view.remainingMs).toBe(2000);
  });

  it('a zero estimate counts nothing and is delayed at once', () => {
    const view = countdown({
      task: task(),
      session: session({ estimate_ms: 0 }),
      launch: null,
      hubOffsetMs: 0,
      now: CREATED,
    });
    expect(view.phase).toBe('delayed');
    expect(view.progress).toBe(0);
  });

  it('ready: "Ready in" from first_working_at, the working transition or a later state', () => {
    const first = countdown({
      task: task(),
      session: session({ state: 'working', first_working_at: at(4200) }, [step(1, 'working', 4300)]),
      launch: launch(),
      hubOffsetMs: null,
      now: CREATED + 9000,
    });
    expect(first).toMatchObject({ phase: 'ready', readyInMs: 4200, current: null });
    expect(first.steps.every((s) => s.status === 'done')).toBe(true);
    const transition = countdown({
      task: task(),
      session: session({ state: 'idle' }, [step(1, 'working', 5100)]),
      launch: null,
      hubOffsetMs: null,
      now: CREATED,
    });
    expect(transition.readyInMs).toBe(5100);
    const later = countdown({
      task: task(),
      session: session({ state: 'suspended' }),
      launch: null,
      hubOffsetMs: null,
      now: CREATED,
    });
    expect(later.readyInMs).toBe(0);
  });

  it('failed: the session failure reason, the step where it failed', () => {
    const view = countdown({
      task: task({ state: 'failed', state_reason: 'vm_start_failed' }),
      session: session({ state: 'failed', state_reason: 'vm_start_failed' }, [
        step(1, 'vm_assigning', 300),
        step(2, 'failed', 900, 'vm_start_failed'),
      ]),
      launch: launch(),
      hubOffsetMs: null,
      now: CREATED + 2000,
    });
    expect(view.phase).toBe('failed');
    expect(view.reason).toBe('vm_start_failed');
    expect(view.steps.map((s) => s.status).slice(0, 3)).toEqual(['done', 'failed', 'pending']);
    const fromTask = countdown({
      task: task({ state: 'failed', state_reason: 'agent_exited' }),
      session: null,
      launch: null,
      hubOffsetMs: null,
      now: CREATED,
    });
    expect(fromTask.reason).toBe('agent_exited');
    const noReason = countdown({
      task: task({ state_reason: null }),
      session: session({ state: 'failed', state_reason: null }),
      launch: null,
      hubOffsetMs: null,
      now: CREATED,
    });
    expect(noReason.reason).toBeNull();
    const failedAfterWork = countdown({
      task: task(),
      session: session({ state: 'failed', first_working_at: at(100) }),
      launch: null,
      hubOffsetMs: null,
      now: CREATED,
    });
    expect(failedAfterWork.phase).toBe('failed');
  });

  it('marks skipped steps done and keeps the current step within the list', () => {
    const view = countdown({
      task: task(),
      session: session({ state: 'prompt_delivering' }, [
        step(1, 'requested', 0),
        step(2, 'vm_ready', 100),
        step(3, 'vm_ready', 150),
      ]),
      launch: null,
      hubOffsetMs: 0,
      now: CREATED,
    });
    expect(view.steps.find((s) => s.state === 'vm_assigning')).toMatchObject({ status: 'done', at: null });
    expect(view.steps.find((s) => s.state === 'vm_ready')!.elapsedMs).toBe(100);
    expect(view.current).toBe('prompt_delivering');
    expect(STEPS).toHaveLength(7);
    const unknown = countdown({
      task: task(),
      session: session({ state: 'stopping' }),
      launch: null,
      hubOffsetMs: 0,
      now: CREATED,
    });
    expect(unknown.phase).toBe('ready');
  });
});

describe('settled, announcement, preparing', () => {
  it('knows a finished start without the time', () => {
    expect(settled(task(), null)).toBe(false);
    expect(settled(task({ state: 'failed' }), null)).toBe(true);
    expect(settled(task(), session())).toBe(false);
    expect(settled(task(), session({ state: 'failed' }))).toBe(true);
    expect(settled(task(), session({ state: 'working' }))).toBe(true);
  });

  it('announces the remaining time while counting and the elapsed time later, in steps of 5 s', () => {
    const view = (now: number, state = 'requested', estimate = 8000) =>
      countdown({
        task: task(),
        session: session({ state, estimate_ms: estimate }),
        launch: null,
        hubOffsetMs: 0,
        now,
      });
    expect(announcement(view(CREATED + 2999))).toEqual({ phase: 'counting', step: 'vm_assigning', seconds: 10 });
    expect(announcement(view(CREATED + 3000))).toEqual({ phase: 'counting', step: 'vm_assigning', seconds: 5 });
    expect(announcement(view(CREATED + 9999, 'requested', 1000))).toEqual({
      phase: 'delayed',
      step: 'vm_assigning',
      seconds: 5,
    });
    expect(announcement(view(CREATED + 5000, 'vm_ready')).step).toBe('sandbox_ready');
    expect(announcement(view(CREATED, 'working')).step).toBeNull();
  });

  it('counts down from 10 s while the send runs', () => {
    expect(preparing(1000, 3500)).toEqual({ remainingMs: 7500, progress: 0.75 });
    expect(preparing(1000, 20_000)).toEqual({ remainingMs: 0, progress: 0 });
  });
});
