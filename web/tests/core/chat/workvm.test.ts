// The work VM events (13-work-vm.md sections 8 and 10, ADR-079): the body checks of `workvm.run` (with its optional
// `log`) and `workvm.state` (also `starting` and `stopped`) against the contract, the latest item per job and per test
// run, the truncated output, the runtime sum of a session and a task (a stopped VM ends its running time), its h/min/s
// text, the memoized selectors and the panel phases (also an implicit start and a stopped VM).
import { describe, expect, it } from 'vitest';
import {
  formatRuntime,
  latestRuns,
  outputTruncated,
  parseWorkvmRun,
  parseWorkvmState,
  selectWorkvmRequests,
  selectWorkvmRuns,
  selectWorkvmStates,
  START_TARGET_MS,
  workvmPanel,
  workvmRuntime,
  type WorkvmRun,
  type WorkvmState,
} from '../../../src/core/chat/workvm';
import { createStore } from '../../../src/core/store';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { type OperationRow, putOperations } from '../../../src/core/store/slices/operations';

const run = (overrides: Partial<Record<keyof WorkvmRun, unknown>> = {}): WorkvmRun =>
  ({
    workvm_id: 'wvm-0123abcd',
    job_id: 'j-0123456789ab',
    kind: 'exec',
    command: 'make test',
    state: 'exited',
    exit_code: 0,
    started_at: '2026-11-21T12:00:00.000Z',
    ended_at: '2026-11-21T12:00:02.500Z',
    duration_ms: 2500,
    output: 'ok\n',
    output_bytes: 3,
    ...overrides,
  }) as WorkvmRun;

const vmState = (overrides: Partial<Record<keyof WorkvmState, unknown>> = {}): WorkvmState =>
  ({
    workvm_id: 'wvm-0123abcd',
    state: 'created',
    scope: 'task',
    size: 'standard',
    cpus: 4,
    memory_mib: 8192,
    disk_gib: 50,
    implicit: false,
    reason: null,
    started_at: '2026-11-21T12:00:00.000Z',
    ended_at: null,
    runtime_ms: 0,
    ...overrides,
  }) as WorkvmState;

describe('parseWorkvmRun', () => {
  it('accepts a contract body, also a running one', () => {
    expect(parseWorkvmRun(run())).toEqual(run());
    const running = run({ state: 'running', exit_code: null, ended_at: null, kind: 'browse' });
    expect(parseWorkvmRun(running)).toBe(running);
    expect(parseWorkvmRun(run({ command: '😀'.repeat(4096) }))).not.toBeNull();
  });

  it('accepts the optional log, also null', () => {
    const log = { object_id: OBJECT, bytes: 300_000, stored_bytes: 300_000, truncated: false };
    expect(parseWorkvmRun({ ...run(), log })).not.toBeNull();
    expect(parseWorkvmRun({ ...run(), log: null })).not.toBeNull();
  });

  it.each([
    ['not an object', 'x'],
    ['a missing field', { object_id: OBJECT, bytes: 1, stored_bytes: 1 }],
    ['an extra field', { object_id: OBJECT, bytes: 1, stored_bytes: 1, truncated: false, more: 1 }],
    ['a bad object id', { object_id: 'x', bytes: 1, stored_bytes: 1, truncated: false }],
    ['negative bytes', { object_id: OBJECT, bytes: -1, stored_bytes: 1, truncated: false }],
    ['a non-boolean truncated', { object_id: OBJECT, bytes: 1, stored_bytes: 1, truncated: 'no' }],
  ])('refuses a log that is %s', (_, log) => {
    expect(parseWorkvmRun({ ...run(), log })).toBeNull();
  });

  it.each([
    ['not an object', null],
    ['an array', []],
    ['a missing field', (({ output: _, ...rest }) => rest)(run())],
    ['an extra field', { ...run(), extra: 1 }],
    ['a bad workvm id', run({ workvm_id: 'wvm-XYZ' })],
    ['a bad job id', run({ job_id: 'j-1' })],
    ['a non-string job id', run({ job_id: 7 })],
    ['an unknown kind', run({ kind: 'shell' })],
    ['an unknown state', run({ state: 'done' })],
    ['a long command', run({ command: 'x'.repeat(4097) })],
    ['a non-string command', run({ command: 1 })],
    ['an exit code above 255', run({ exit_code: 256 })],
    ['a negative exit code', run({ exit_code: -1 })],
    ['a fractional duration', run({ duration_ms: 1.5 })],
    ['a bad start', run({ started_at: '2026-11-21' })],
    ['a bad end', run({ ended_at: 'later' })],
    ['a long output', run({ output: 'x'.repeat(65537) })],
    ['negative output bytes', run({ output_bytes: -1 })],
  ])('refuses %s', (_, body) => {
    expect(parseWorkvmRun(body)).toBeNull();
  });
});

describe('parseWorkvmState', () => {
  it('accepts created, starting, stopped and deleted bodies', () => {
    expect(parseWorkvmState(vmState())).toEqual(vmState());
    expect(parseWorkvmState(vmState({ state: 'starting', implicit: true }))).not.toBeNull();
    const stopped = vmState({ state: 'stopped', reason: 'session_paused', runtime_ms: 5 });
    expect(parseWorkvmState(stopped)).toBe(stopped);
    expect(parseWorkvmState(vmState({ state: 'deleted', reason: 'credit_exhausted' }))).not.toBeNull();
    const deleted = vmState({
      state: 'deleted',
      reason: 'idle',
      ended_at: '2026-11-21T12:30:00.000Z',
      runtime_ms: 1_800_000,
      implicit: true,
      scope: 'session',
      size: 'xlarge',
    });
    expect(parseWorkvmState(deleted)).toBe(deleted);
  });

  it.each([
    ['not an object', 'x'],
    ['an unknown state', vmState({ state: 'paused' })],
    ['an unknown scope', vmState({ scope: 'org' })],
    ['an unknown size', vmState({ size: 'huge' })],
    ['too many cpus', vmState({ cpus: 65 })],
    ['too little memory', vmState({ memory_mib: 256 })],
    ['no disk', vmState({ disk_gib: 0 })],
    ['a non-boolean implicit', vmState({ implicit: 'yes' })],
    ['an unknown reason', vmState({ reason: 'bored' })],
    ['a missing runtime', (({ runtime_ms: _, ...rest }) => rest)(vmState())],
  ])('refuses %s', (_, body) => {
    expect(parseWorkvmState(body)).toBeNull();
  });
});

describe('outputTruncated', () => {
  it('compares the UTF-8 size of the tail with the total', () => {
    expect(outputTruncated(run({ output: 'ä', output_bytes: 2 }))).toBe(false);
    expect(outputTruncated(run({ output: 'tail', output_bytes: 100_000 }))).toBe(true);
  });
});

describe('latestRuns', () => {
  it('keeps the latest item of a job, other items and invalid bodies in place', () => {
    const items = [
      { kind: 'workvm.run', body: run({ state: 'running', exit_code: null, ended_at: null }), id: 1 },
      { kind: 'session.output', body: { text: 'x' }, id: 2 },
      { kind: 'workvm.run', body: run({ job_id: 'j-bbbbbbbbbbbb', state: 'running' }), id: 3 },
      { kind: 'workvm.run', body: null, id: 4 },
      { kind: 'workvm.run', body: null, id: 5 },
      { kind: 'workvm.run', body: run(), id: 6 },
    ];
    expect(latestRuns(items).map((item) => item.id)).toEqual([2, 3, 4, 5, 6]);
    expect(latestRuns([])).toEqual([]);
  });

  it('keeps the latest item of a test run, and keeps jobs and runs apart', () => {
    const test = (state: string) => ({ ...testRunBody(), state });
    const items = [
      { kind: 'test.run', body: test('running'), id: 1 },
      { kind: 'workvm.run', body: run(), id: 2 },
      { kind: 'test.run', body: { broken: true }, id: 3 },
      { kind: 'test.run', body: test('failed'), id: 4 },
    ];
    expect(latestRuns(items).map((item) => item.id)).toEqual([2, 3, 4]);
  });
});

describe('workvmRuntime', () => {
  const now = Date.parse('2026-11-21T13:00:00.000Z');

  it('is zero without a VM', () => {
    expect(workvmRuntime([], now)).toEqual({ ms: 0, vms: 0, running: false });
  });

  it('sums deleted runtimes and the running time of a created VM', () => {
    const states = [
      vmState({ workvm_id: 'wvm-00000001' }),
      vmState({ workvm_id: 'wvm-00000001', state: 'deleted', reason: 'released', runtime_ms: 90_000 }),
      // deleted first, then its created event arrives: the deleted one wins
      vmState({ workvm_id: 'wvm-00000002', state: 'deleted', reason: 'failed', runtime_ms: 10_000 }),
      vmState({ workvm_id: 'wvm-00000002' }),
      vmState({ workvm_id: 'wvm-00000003', started_at: '2026-11-21T12:59:00.000Z' }),
    ];
    expect(workvmRuntime(states, now)).toEqual({ ms: 160_000, vms: 3, running: true });
  });

  it('ends the running time of a stopped VM with its runtime, and counts it again once it is created again', () => {
    const stopped = vmState({ state: 'stopped', reason: 'session_paused', runtime_ms: 70_000 });
    expect(workvmRuntime([vmState(), stopped], now)).toEqual({ ms: 70_000, vms: 1, running: false });
    const again = vmState({ started_at: '2026-11-21T12:59:00.000Z' });
    expect(workvmRuntime([vmState(), stopped, vmState({ state: 'starting' }), again], now)).toEqual({
      ms: 60_000,
      vms: 1,
      running: true,
    });
    // a VM that is only starting has no runtime yet
    expect(workvmRuntime([vmState({ state: 'starting' })], now)).toEqual({ ms: 0, vms: 0, running: false });
  });

  it('never counts negative time of a VM that started after now', () => {
    expect(workvmRuntime([vmState({ started_at: '2026-11-21T14:00:00.000Z' })], now)).toEqual({
      ms: 0,
      vms: 1,
      running: true,
    });
  });
});

describe('formatRuntime', () => {
  it.each([
    [0, '0 s'],
    [-5, '0 s'],
    [999, '0 s'],
    [45_000, '45 s'],
    [240_000, '4 min'],
    [7_503_400, '2 h 5 min 3 s'],
    [90_000_000, '25 h'],
  ])('formats %d ms as %s', (ms, text) => {
    expect(formatRuntime(ms)).toBe(text);
  });
});

const OBJECT = '0190f000-0000-7000-8000-00000000f001';
const testRunBody = () => ({
  run_id: 'tr-0123456789ab',
  operation_id: '0190f000-0000-7000-8000-00000000c009',
  kind: 'unit',
  command: 'npm test',
  cwd: '/workspace',
  size: 'standard',
  state: 'passed',
  exit_code: 0,
  started_at: '2026-11-21T12:00:00.000Z',
  ended_at: '2026-11-21T12:01:00.000Z',
  duration_ms: 60_000,
  ready_ms: 4_000,
  summary: null,
  log: null,
  tail: '',
  reason: null,
});

const WG = 'wg';
const TASK = 'task-1';
let seq = 1;
const entry = (kind: string, body: unknown, session: string, options: Partial<EventEntry> = {}): EventEntry => {
  const header = { id: `id-${seq}`, kind, task: TASK, session, author: 'd1', ts: '2026-11-21T12:00:01.000Z' };
  return {
    org: 'org',
    scopeId: WG,
    scopeSeq: seq++,
    receivedAt: null,
    event: { header },
    status: 'verified',
    code: null,
    header,
    body,
    ...options,
  };
};

describe('selectWorkvmStates', () => {
  it('collects the valid states of the task and the VMs of the session, memoized on the scope', () => {
    const store = createStore();
    const select = selectWorkvmStates(WG, TASK, 'a');
    expect(select(store.getState())).toEqual({ task: [], session: [] });
    const taskVm = vmState({ workvm_id: 'wvm-0000000a' });
    const taskVmDeleted = vmState({ workvm_id: 'wvm-0000000a', state: 'deleted', reason: 'idle', runtime_ms: 5 });
    const otherVm = vmState({ workvm_id: 'wvm-0000000b', scope: 'session' });
    putEvents(store, WG, [
      entry('workvm.state', taskVm, 'a'),
      entry('workvm.state', taskVmDeleted, 'b'),
      entry('workvm.state', otherVm, 'b'),
      entry('workvm.state', { broken: true }, 'a'),
      entry('workvm.run', run(), 'a'),
      entry('workvm.state', vmState(), 'a', { header: { kind: 'workvm.state', task: 'other', session: 'a' } }),
      entry('workvm.state', null, 'a', { status: 'unverifiable', header: null }),
    ]);
    const states = select(store.getState());
    expect(states.task).toEqual([taskVm, taskVmDeleted, otherVm]);
    expect(states.session).toEqual([taskVm, taskVmDeleted]);
    expect(select(store.getState())).toBe(states);
  });

  it('counts the task VM another session obtained as a VM of the session once it ran a job there', () => {
    const store = createStore();
    const select = selectWorkvmStates(WG, TASK, 'c');
    const taskVm = vmState({ workvm_id: 'wvm-0000000d' });
    putEvents(store, WG, [
      entry('workvm.state', taskVm, 'a'),
      entry('workvm.state', vmState({ workvm_id: 'wvm-0000000e' }), 'b'),
      entry('workvm.run', run({ workvm_id: 'wvm-0000000e' }), 'b'),
      entry('workvm.run', { broken: true }, 'c'),
      entry('workvm.run', run({ workvm_id: 'wvm-0000000d' }), 'c'),
    ]);
    expect(select(store.getState()).session).toEqual([taskVm]);
  });
});

describe('selectWorkvmRuns', () => {
  it('keeps the latest valid body per job of the session, newest start first, memoized on the scope', () => {
    const store = createStore();
    const select = selectWorkvmRuns(WG, TASK, 'a');
    expect(select(store.getState())).toEqual([]);
    const first = run({ job_id: 'j-00000000000a', state: 'running', exit_code: null, ended_at: null });
    const firstDone = run({ job_id: 'j-00000000000a' });
    const second = run({ job_id: 'j-00000000000b', started_at: '2026-11-21T12:01:00.000Z' });
    const sameStart = run({ job_id: 'j-00000000000c', started_at: '2026-11-21T12:01:00.000Z' });
    putEvents(store, WG, [
      entry('workvm.run', first, 'a'),
      entry('workvm.run', second, 'a'),
      entry('workvm.run', sameStart, 'a'),
      entry('workvm.run', firstDone, 'a'),
      entry('workvm.run', run({ job_id: 'j-00000000000d' }), 'b'),
      entry('workvm.run', { broken: true }, 'a'),
      entry('workvm.state', vmState(), 'a'),
      entry('workvm.run', run(), 'a', { header: { kind: 'workvm.run', task: 'other', session: 'a' } }),
      entry('workvm.run', null, 'a', { status: 'unverifiable', header: null }),
    ]);
    const runs = select(store.getState());
    expect(runs).toEqual([second, sameStart, firstDone]);
    expect(select(store.getState())).toBe(runs);
  });
});

const T0 = Date.parse('2026-11-21T12:00:00.000Z');
const request = (overrides: Partial<OperationRow> = {}): OperationRow => ({
  operation_id: '0190f000-0000-7000-8000-00000000c001',
  org_id: 'org',
  wg_id: WG,
  task_id: TASK,
  session_id: 'a',
  device_id: 'd1',
  actor_id: 'd1',
  type: 'workvm.request',
  state: 'executing',
  error_code: null,
  retryable: false,
  duplicate_of: null,
  attempt: 1,
  params_event_id: null,
  result_event_id: null,
  created_at: '2026-11-21T12:00:00.000Z',
  decided_at: null,
  started_at: null,
  finished_at: null,
  ...overrides,
});

describe('selectWorkvmRequests', () => {
  it('selects the work VM requests of the session, memoized on the rows', () => {
    const store = createStore();
    const select = selectWorkvmRequests('a');
    expect(select(store.getState())).toEqual([]);
    const own = request();
    putOperations(store, [
      own,
      request({ operation_id: '0190f000-0000-7000-8000-00000000c002', session_id: 'b' }),
      request({ operation_id: '0190f000-0000-7000-8000-00000000c003', type: 'workvm.release' }),
    ]);
    const requests = select(store.getState());
    expect(requests).toEqual([own]);
    expect(select(store.getState())).toBe(requests);
  });
});

describe('workvmPanel', () => {
  it('is none without a VM and without an open request', () => {
    expect(workvmPanel([], [request({ state: 'failed' })], T0)).toEqual({
      phase: 'none',
      vm: null,
      elapsedMs: 0,
      remainingMs: 0,
      overdue: false,
    });
  });

  it('counts down to the target while a request is open, from the oldest open request', () => {
    const open = [
      request({ created_at: '2026-11-21T12:00:02.000Z' }),
      request({ operation_id: 'x', state: 'received' }),
    ];
    expect(workvmPanel([], open, T0 + 3_500)).toEqual({
      phase: 'starting',
      vm: null,
      elapsedMs: 3_500,
      remainingMs: START_TARGET_MS - 3_500,
      overdue: false,
    });
    expect(workvmPanel([], open, T0 - 1_000).elapsedMs).toBe(0);
    const late = workvmPanel([], open, T0 + 12_000);
    expect(late.remainingMs).toBe(0);
    expect(late.overdue).toBe(true);
  });

  it('shows the running VM, even while another request is open', () => {
    const created = vmState({ workvm_id: 'wvm-0000000b', started_at: '2026-11-21T11:00:00.000Z' });
    const older = vmState({ started_at: '2026-11-21T10:00:00.000Z' });
    const olderDeleted = vmState({ ...older, state: 'deleted', reason: 'idle', runtime_ms: 9 });
    expect(workvmPanel([older, olderDeleted, created], [request()], T0)).toMatchObject({
      phase: 'running',
      vm: created,
    });
  });

  it('shows the last deleted VM, and keeps it while a new request starts', () => {
    const a = vmState({ started_at: '2026-11-21T10:00:00.000Z' });
    const aDeleted = { ...a, state: 'deleted', reason: 'idle', runtime_ms: 9 } as WorkvmState;
    const b = vmState({ workvm_id: 'wvm-0000000b', started_at: '2026-11-21T11:00:00.000Z' });
    const bDeleted = { ...b, state: 'deleted', reason: 'released', runtime_ms: 7 } as WorkvmState;
    expect(workvmPanel([b, bDeleted, a, aDeleted], [], T0)).toMatchObject({ phase: 'deleted', vm: bDeleted });
    expect(workvmPanel([b, bDeleted], [request()], T0)).toMatchObject({ phase: 'starting', vm: bDeleted });
  });

  it('counts down from an implicit start until the VM is created or deleted', () => {
    const starting = vmState({ state: 'starting', implicit: true, started_at: '2026-11-21T12:00:01.000Z' });
    expect(workvmPanel([starting], [], T0 + 4_000)).toEqual({
      phase: 'starting',
      vm: starting,
      elapsedMs: 3_000,
      remainingMs: START_TARGET_MS - 3_000,
      overdue: false,
    });
    // the older of an open request and an implicit start counts
    expect(workvmPanel([starting], [request()], T0 + 4_000).elapsedMs).toBe(4_000);
    const created = vmState({ implicit: true, started_at: '2026-11-21T12:00:01.000Z' });
    expect(workvmPanel([starting, created], [], T0 + 4_000)).toMatchObject({ phase: 'running', vm: created });
    const failed = { ...starting, state: 'deleted', reason: 'failed' } as WorkvmState;
    expect(workvmPanel([starting, failed], [], T0 + 4_000)).toMatchObject({ phase: 'deleted', vm: failed });
  });

  it('shows a stopped VM as stopped, and starting again once the resumed session starts it', () => {
    const stopped = vmState({ state: 'stopped', reason: 'session_paused', runtime_ms: 9 });
    expect(workvmPanel([vmState(), stopped], [], T0)).toMatchObject({ phase: 'stopped', vm: stopped });
    const again = vmState({ state: 'starting', started_at: '2026-11-21T12:10:00.000Z' });
    expect(workvmPanel([vmState(), stopped, again], [], T0 + 600_000)).toMatchObject({ phase: 'starting' });
  });

  it('keeps a deleted VM deleted when its created event arrives after the deleted one', () => {
    const a = vmState();
    const aDeleted = { ...a, state: 'deleted', reason: 'idle', runtime_ms: 9 } as WorkvmState;
    expect(workvmPanel([aDeleted, a], [], T0)).toMatchObject({ phase: 'deleted', vm: aDeleted });
  });
});
