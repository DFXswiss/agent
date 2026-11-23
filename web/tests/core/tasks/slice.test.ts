// U-WEB-ui-tasks (slice `tasks` and the frame registry): row validation, lists newest first with filter placement,
// pages, sessions with transitions and `estimatedAt`, launches, the `hello`/`task`/`session` frames and their
// discovery from the slice modules.
import { describe, expect, it } from 'vitest';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import { createStore } from '../../../src/core/store';
import {
  frames,
  isSessionRow,
  isTaskRow,
  isTransition,
  listFailed,
  listKey,
  listLoading,
  listPage,
  newestFirst,
  putLaunch,
  putSession,
  putTasks,
  setHubOffset,
  staleSession,
  staleTask,
  taskList,
  type TaskRow,
} from '../../../src/core/store/slices/tasks';
import { sessionRow, taskRow } from './world';

const T = { org: 'o', wg: 'w', me: { id: 'u' } } as never;
const row = (id: string, created: string, overrides: Record<string, unknown> = {}) =>
  taskRow(T, { task_id: id, created_at: created, ...overrides }) as TaskRow;

describe('validation', () => {
  it('accepts well-formed rows only', () => {
    expect(isTaskRow(taskRow(T))).toBe(true);
    expect(isTaskRow({ ...taskRow(T), state: 1 })).toBe(false);
    expect(isTaskRow({ ...taskRow(T), title_event_id: 1 })).toBe(false);
    expect(isTaskRow(null)).toBe(false);
    expect(isSessionRow(sessionRow(T))).toBe(true);
    expect(isSessionRow(sessionRow(T, { estimate_ms: null }))).toBe(true);
    expect(isSessionRow(sessionRow(T, { estimate_ms: -1 }))).toBe(false);
    expect(isSessionRow(sessionRow(T, { device_id: 3 }))).toBe(false);
    expect(isSessionRow(sessionRow(T, { state: null }))).toBe(false);
    expect(isSessionRow([])).toBe(false);
    const transition = { seq: 1, state: 'vm_ready', reason: null, at: 'a', received_at: 'b' };
    expect(isTransition(transition)).toBe(true);
    for (const broken of [{ seq: -1 }, { state: 1 }, { reason: 1 }, { at: 1 }, { received_at: null }]) {
      expect(isTransition({ ...transition, ...broken })).toBe(false);
    }
    expect(isTransition('x')).toBe(false);
  });
});

describe('lists', () => {
  it('keeps the newest first with the task ID as tie-breaker', () => {
    const tasks = {
      a: row('a', '2026-11-21T12:00:00.000Z'),
      b: row('b', '2026-11-21T12:00:01.000Z'),
      c: row('c', '2026-11-21T12:00:00.000Z'),
    };
    expect(newestFirst(tasks, ['a', 'b', 'c', 'a'])).toEqual(['b', 'c', 'a']);
  });

  it('returns the same idle list object until a list is loaded', () => {
    const store = createStore();
    const idle = taskList(store.getState().tasks, 'w', null);
    expect(idle).toBe(taskList(store.getState().tasks, 'w', null));
    expect(idle).toMatchObject({ wg: 'w', state: null, ids: [], status: 'idle', nextBefore: null, code: null });
    expect(listKey('w', 'working')).toBe('w working');
  });

  it('loads pages, records failures and appends further pages', () => {
    const store = createStore();
    listLoading(store, 'w', null);
    expect(taskList(store.getState().tasks, 'w', null).status).toBe('loading');
    listFailed(store, 'w', null, 'unavailable');
    expect(taskList(store.getState().tasks, 'w', null)).toMatchObject({ status: 'error', code: 'unavailable' });
    listPage(store, {
      wg: 'w',
      state: null,
      rows: [row('b', '2026-11-21T12:00:01.000Z')],
      nextBefore: 'c1',
      first: true,
    });
    listPage(store, {
      wg: 'w',
      state: null,
      rows: [row('a', '2026-11-21T12:00:00.000Z')],
      nextBefore: null,
      first: false,
    });
    expect(taskList(store.getState().tasks, 'w', null)).toMatchObject({
      ids: ['b', 'a'],
      nextBefore: null,
      status: 'ready',
    });
    listPage(store, { wg: 'w', state: null, rows: [], nextBefore: null, first: true });
    expect(taskList(store.getState().tasks, 'w', null).ids).toEqual([]);
    listFailed(store, 'x', 'idle', 'forbidden');
    listLoading(store, 'y', null);
    listPage(store, { wg: 'z', state: 'failed', rows: [], nextBefore: null, first: false });
    expect(Object.keys(store.getState().tasks.lists).sort()).toEqual(['w ', 'x idle', 'y ', 'z failed']);
  });

  it('moves changed rows between the loaded lists of their workgroup', () => {
    const store = createStore();
    putTasks(store, []);
    const before = store.getState();
    putTasks(store, []);
    expect(store.getState()).toBe(before);
    listPage(store, { wg: 'w', state: null, rows: [], nextBefore: null, first: true });
    listPage(store, { wg: 'w', state: 'working', rows: [], nextBefore: null, first: true });
    listPage(store, { wg: 'w', state: 'failed', rows: [], nextBefore: null, first: true });
    listPage(store, { wg: 'other', state: null, rows: [], nextBefore: null, first: true });
    putTasks(store, [row('a', '2026-11-21T12:00:00.000Z', { state: 'working' })]);
    const lists = () => {
      const state = store.getState().tasks;
      return [null, 'working', 'failed'].map((filter) => taskList(state, 'w', filter).ids);
    };
    expect(lists()).toEqual([['a'], ['a'], []]);
    putTasks(store, [row('a', '2026-11-21T12:00:00.000Z', { state: 'failed' })]);
    expect(lists()).toEqual([['a'], [], ['a']]);
    putTasks(store, [row('b', '2026-11-21T12:00:05.000Z', { state: 'idle' })]);
    expect(lists()).toEqual([['b', 'a'], [], ['a']]);
    expect(taskList(store.getState().tasks, 'other', null).ids).toEqual([]);
  });
});

describe('stale rows', () => {
  it('ignores a task row older than the stored one', () => {
    const store = createStore();
    const newer = row('a', '2026-11-21T12:00:00.000Z', {
      state: 'working',
      last_activity_at: '2026-11-21T12:00:05.000Z',
      state_at: '2026-11-21T12:00:05.000Z',
    });
    listPage(store, { wg: 'w', state: 'starting', rows: [], nextBefore: null, first: true });
    putTasks(store, [newer]);
    const before = store.getState();
    putTasks(store, [{ ...newer, state: 'starting', last_activity_at: '2026-11-21T12:00:01.000Z' }]);
    putTasks(store, [{ ...newer, state: 'starting', state_at: '2026-11-21T12:00:01.000Z' }]);
    expect(store.getState()).toBe(before);
    expect(staleTask(undefined, newer)).toBe(false);
    putTasks(store, [{ ...newer, state: 'idle', state_at: '2026-11-21T12:00:06.000Z' }]);
    expect(store.getState().tasks.tasks.a!.state).toBe('idle');
    // one batch with a stale and a new row: only the new one moves into the lists
    putTasks(store, [
      { ...newer, state: 'starting', last_activity_at: '2026-11-21T11:00:00.000Z' },
      row('b', '2026-11-21T12:00:09.000Z', { state: 'starting' }),
    ]);
    expect(taskList(store.getState().tasks, 'w', 'starting').ids).toEqual(['b']);
    listPage(store, {
      wg: 'w',
      state: null,
      rows: [{ ...newer, state: 'scheduling', last_activity_at: '2026-11-21T11:00:00.000Z' }],
      nextBefore: null,
      first: true,
    });
    expect(store.getState().tasks.tasks.a!.state).toBe('idle');
    expect(taskList(store.getState().tasks, 'w', null).ids).toEqual(['a']);
  });

  it('keeps the newer session row: transitions, final states, first_working_at, start-up order', () => {
    const t1 = { seq: 1, state: 'requested', reason: null, at: 'a', received_at: 'b' };
    const t3 = { seq: 3, state: 'vm_ready', reason: null, at: 'a', received_at: 'b' };
    const known = (state: string, extra: Record<string, unknown> = {}) =>
      ({ row: sessionRow(T, { state, ...extra }), transitions: [t1, t3], estimatedAt: null }) as never;
    const incoming = (state: string, extra: Record<string, unknown> = {}) =>
      sessionRow(T, { state, ...extra }) as never;
    expect(staleSession(undefined, incoming('requested'), [])).toBe(false);
    expect(staleSession(known('vm_ready'), incoming('requested'), [t1])).toBe(true);
    expect(staleSession(known('failed'), incoming('vm_ready'), [])).toBe(true);
    expect(staleSession(known('working', { first_working_at: 'x' }), incoming('idle'), [])).toBe(true);
    expect(staleSession(known('vm_ready'), incoming('requested'), [])).toBe(true);
    expect(staleSession(known('vm_ready'), incoming('agent_starting'), [])).toBe(false);
    expect(staleSession(known('vm_ready'), incoming('working'), [])).toBe(false);
    expect(staleSession(known('vm_ready'), incoming('sandbox_ready'), [{ ...t3, seq: 4 }])).toBe(false);
    const store = createStore();
    putSession(store, sessionRow(T, { state: 'failed', estimate_ms: null }) as never, [t1], 1);
    putSession(store, sessionRow(T, { state: 'requested', estimate_ms: 3000 }) as never, [], 2);
    const entry = store.getState().tasks.sessions[sessionRow(T).session_id as string]!;
    expect(entry.row.state).toBe('failed');
    expect(entry.estimatedAt).toBeNull();
    const empty = { row: sessionRow(T, { state: 'idle' }), transitions: [], estimatedAt: null } as never;
    expect(staleSession(empty, incoming('idle'), [t1])).toBe(false);
  });
});

describe('sessions and launches', () => {
  it('merges transitions by seq and records when a waiting session got its estimate', () => {
    const store = createStore();
    const t1 = { seq: 1, state: 'requested', reason: null, at: 'a', received_at: 'b' };
    const t2 = { seq: 2, state: 'vm_ready', reason: null, at: 'a', received_at: 'b' };
    putSession(store, sessionRow(T, { estimate_ms: null }) as never, [t2], 100);
    putSession(store, sessionRow(T, { estimate_ms: null }) as never, [t1, { ...t2, state: 'x' }], 200);
    const id = sessionRow(T).session_id as string;
    expect(store.getState().tasks.sessions[id]).toMatchObject({ transitions: [t1, t2], estimatedAt: null });
    putSession(store, sessionRow(T, { estimate_ms: 3000 }) as never, [], 300);
    expect(store.getState().tasks.sessions[id]!.estimatedAt).toBe(300);
    putSession(store, sessionRow(T, { estimate_ms: 2500 }) as never, [], 400);
    expect(store.getState().tasks.sessions[id]!.estimatedAt).toBe(300);
    putSession(store, sessionRow(T, { session_id: 'fresh', estimate_ms: 3000 }) as never, [], 500);
    expect(store.getState().tasks.sessions.fresh!.estimatedAt).toBeNull();
    putLaunch(store, 't', { sessionId: 's', localAt: 1, estimateMs: null, assignedMs: 2 });
    expect(store.getState().tasks.launches.t).toEqual({ sessionId: 's', localAt: 1, estimateMs: null, assignedMs: 2 });
  });
});

describe('frames', () => {
  it('applies hello (hub clock offset), task and session frames; ignores malformed ones', () => {
    const store = createStore();
    frames.hello!(store, { hub_time: '2026-11-21T12:00:02.000Z' }, Date.parse('2026-11-21T12:00:00.000Z'));
    expect(store.getState().tasks.hubOffsetMs).toBe(2000);
    const same = store.getState();
    setHubOffset(store, 2000);
    expect(store.getState()).toBe(same);
    for (const data of [null, { hub_time: 1 }, { hub_time: 'nonsense' }]) frames.hello!(store, data, 0);
    expect(store.getState().tasks.hubOffsetMs).toBe(2000);
    frames.task!(store, taskRow(T), 0);
    frames.task!(store, { task_id: 1 }, 0);
    expect(Object.keys(store.getState().tasks.tasks)).toEqual([taskRow(T).task_id]);
    const transition = { seq: 1, state: 'requested', reason: null, at: 'a', received_at: 'b' };
    frames.session!(store, { session: sessionRow(T), transition }, 7);
    frames.session!(store, { session: sessionRow(T, { session_id: 's2' }), transition: null }, 7);
    frames.session!(store, { session: {} }, 7);
    frames.session!(store, 'x', 7);
    const sessions = store.getState().tasks.sessions;
    expect(sessions[sessionRow(T).session_id as string]!.transitions).toEqual([transition]);
    expect(sessions.s2!.transitions).toEqual([]);
    expect(Object.keys(sessions)).toHaveLength(2);
  });

  it('discovers the frames of the slice modules and isolates failing handlers', () => {
    expect(frameHandlers().some((set) => 'session' in set)).toBe(true);
    const seen: string[] = [];
    const modules = {
      './b.ts': { frames: { task: () => seen.push('b') } },
      './a.ts': {
        frames: {
          task: () => {
            throw new Error('bug');
          },
        },
      },
      './c.ts': { slice: {} },
      './d.ts': 'not a module',
    };
    const handlers = frameHandlers(modules);
    expect(handlers).toHaveLength(2);
    const store = createStore();
    expect(applyFrame(store, 'task', {}, 0, handlers)).toBe(2);
    expect(seen).toEqual(['b']);
    expect(applyFrame(store, 'operation', {}, 0, handlers)).toBe(0);
    expect(applyFrame(store, 'toString', {}, 0, handlers)).toBe(0);
    expect(() => frameHandlers({ './x.ts': { frames: { task: 1 } } })).toThrow('./x.ts: invalid export "frames"');
    expect(() => frameHandlers({ './x.ts': { frames: 'x' } })).toThrow('invalid export');
  });

  it('applies a frame with the discovered handlers by default', () => {
    const store = createStore();
    expect(applyFrame(store, 'task', taskRow(T), 0)).toBe(1);
    expect(applyFrame(store, 'task', taskRow(T), 0)).toBe(1);
    expect(store.getState().tasks.tasks[taskRow(T).task_id as string]).toBeDefined();
  });
});
