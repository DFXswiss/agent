// The test runs of a session (ADR-080): the body check of `test.run` and of the `test.run` operation result against
// the contracts, the red state, the summary parts and the memoized selector of the latest body per run.
import { describe, expect, it } from 'vitest';
import {
  parseTestRun,
  parseTestRunResult,
  runFailed,
  selectTestRuns,
  summaryCounts,
  type TestRun,
} from '../../../src/core/chat/testrun';
import { createStore } from '../../../src/core/store';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';

const OBJECT = '0190f000-0000-7000-8000-00000000f001';
const LOG = { object_id: OBJECT, bytes: 10, stored_bytes: 10, truncated: false };

const body = (overrides: Partial<Record<keyof TestRun, unknown>> = {}): TestRun =>
  ({
    run_id: 'tr-0123456789ab',
    operation_id: '0190f000-0000-7000-8000-00000000c009',
    kind: 'unit',
    command: 'npm test',
    cwd: '/workspace',
    size: 'standard',
    state: 'failed',
    exit_code: 1,
    started_at: '2026-11-21T12:00:00.000Z',
    ended_at: '2026-11-21T12:01:00.000Z',
    duration_ms: 60_000,
    ready_ms: 4_000,
    summary: { passed: 12, failed: 3, skipped: null, errors: 0 },
    log: LOG,
    tail: 'FAIL x\n',
    reason: null,
    ...overrides,
  }) as TestRun;

const result = (overrides: Record<string, unknown> = {}) => {
  const { operation_id: _o, kind: _k, command: _c, cwd: _w, size: _s, started_at: _a, ended_at: _e, ...rest } = body();
  return { ...rest, ...overrides };
};

describe('parseTestRun', () => {
  it('accepts a contract body, also a running one without summary and log', () => {
    expect(parseTestRun(body())).toEqual(body());
    const running = body({ state: 'running', exit_code: null, ended_at: null, summary: null, log: null, tail: '' });
    expect(parseTestRun(running)).toBe(running);
    expect(parseTestRun(body({ state: 'error', reason: 'start_failed', kind: 'custom' }))).not.toBeNull();
  });

  it.each([
    ['not an object', null],
    ['a missing field', (({ tail: _, ...rest }) => rest)(body())],
    ['an extra field', { ...body(), extra: 1 }],
    ['a bad run id', body({ run_id: 'j-0123456789ab' })],
    ['a bad operation id', body({ operation_id: 'op' })],
    ['an unknown kind', body({ kind: 'smoke' })],
    ['an unknown state', body({ state: 'green' })],
    ['an unknown reason', body({ reason: 'bored' })],
    ['an unknown size', body({ size: 'huge' })],
    ['a long command', body({ command: 'x'.repeat(4097) })],
    ['a long tail', body({ tail: 'x'.repeat(8193) })],
    ['a summary with a missing count', body({ summary: { passed: 1, failed: 0, skipped: 0 } })],
    ['a summary with a negative count', body({ summary: { passed: -1, failed: 0, skipped: 0, errors: 0 } })],
    ['a bad log', body({ log: { object_id: OBJECT } })],
    ['a bad start', body({ started_at: 'now' })],
  ])('refuses %s', (_, value) => {
    expect(parseTestRun(value)).toBeNull();
  });
});

describe('parseTestRunResult', () => {
  it('accepts the operation result and refuses the event body', () => {
    expect(parseTestRunResult(result())).toEqual(result());
    expect(parseTestRunResult(body())).toBeNull();
    expect(parseTestRunResult(result({ ready_ms: -1 }))).toBeNull();
  });
});

describe('runFailed and summaryCounts', () => {
  it.each([
    ['running', false],
    ['passed', false],
    ['failed', true],
    ['error', true],
    ['timed_out', true],
    ['cancelled', true],
  ])('%s is red: %s', (state, red) => {
    expect(runFailed({ state: state as TestRun['state'] })).toBe(red);
  });

  it('lists the non-zero counts, red ones first', () => {
    expect(summaryCounts(null)).toEqual([]);
    expect(summaryCounts({ passed: 12, failed: 3, skipped: null, errors: 0 })).toEqual([
      ['failed', 3],
      ['passed', 12],
    ]);
    expect(summaryCounts({ passed: 1, failed: null, skipped: 2, errors: 4 })).toEqual([
      ['errors', 4],
      ['passed', 1],
      ['skipped', 2],
    ]);
    expect(summaryCounts({ passed: null, failed: null, skipped: null, errors: null })).toEqual([]);
  });
});

const WG = 'wg';
const TASK = 'task-1';
let seq = 1;
const entry = (kind: string, value: unknown, session: string, options: Partial<EventEntry> = {}): EventEntry => {
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
    body: value,
    ...options,
  };
};

describe('selectTestRuns', () => {
  it('keeps the latest valid body per run of the session, newest start first, memoized on the scope', () => {
    const store = createStore();
    const select = selectTestRuns(WG, TASK, 'a');
    expect(select(store.getState())).toEqual([]);
    const first = body({ run_id: 'tr-00000000000a', state: 'running', exit_code: null, ended_at: null });
    const firstDone = body({ run_id: 'tr-00000000000a' });
    const second = body({ run_id: 'tr-00000000000b', started_at: '2026-11-21T12:05:00.000Z' });
    putEvents(store, WG, [
      entry('test.run', first, 'a'),
      entry('test.run', second, 'a'),
      entry('test.run', firstDone, 'a'),
      entry('test.run', body({ run_id: 'tr-00000000000c' }), 'b'),
      entry('test.run', { broken: true }, 'a'),
      entry('workvm.run', body(), 'a'),
      entry('test.run', body(), 'a', { header: { kind: 'test.run', task: 'other', session: 'a' } }),
      entry('test.run', null, 'a', { status: 'unverifiable', header: null }),
    ]);
    const runs = select(store.getState());
    expect(runs).toEqual([second, firstDone]);
    expect(select(store.getState())).toBe(runs);
  });
});
