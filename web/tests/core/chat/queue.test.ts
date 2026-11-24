// U-WEB-queue (KOR-46, ADR-083 section 3): the queue of a task - parsing the hub's answer, loading it into the store,
// withdrawing a waiting prompt, the signature that triggers a new read, and the debounced, polling watch.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  loadQueue,
  QUEUE_DEBOUNCE_MS,
  QUEUE_POLL_MS,
  queueInputs,
  sameInputs,
  taskQueue,
  watchQueue,
  withdrawPrompt,
} from '../../../src/core/chat/queue';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import type { EventEntry } from '../../../src/core/store/slices/events';
import { putQueue, selectWithdrawal } from '../../../src/core/store/slices/queue';
import { putSession, putTasks, type SessionRow, type TaskRow } from '../../../src/core/store/slices/tasks';
import { FakeClient } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const SESSION = '0190f000-0000-7000-8000-0000000000a1';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const ALICE = '0190f000-0000-7000-8000-0000000000c1';
const WG = 'wg-1';

const prompt = (event: string, overrides: Record<string, unknown> = {}) => ({
  event_id: event,
  author_id: ALICE,
  via_agent: false,
  scope_seq: 5,
  received_at: '2026-11-24T10:00:00.000Z',
  session_id: SESSION,
  device_id: DEVICE,
  state: 'waiting',
  ...overrides,
});

function world() {
  const store = createStore();
  const client = new FakeClient();
  const context = { store, client, now: () => Date.parse('2026-11-24T11:00:00.000Z') };
  return { store, client, context };
}

const task = (overrides: Partial<TaskRow> = {}): TaskRow => ({
  task_id: TASK,
  org_id: 'org-1',
  wg_id: WG,
  owner_id: ALICE,
  state: 'running',
  state_reason: null,
  created_at: '2026-11-24T10:00:00.000Z',
  state_at: '2026-11-24T10:00:00.000Z',
  last_activity_at: '2026-11-24T10:00:00.000Z',
  closed_at: null,
  main_session_id: SESSION,
  title_event_id: null,
  ...overrides,
});

const session = (overrides: Partial<SessionRow> = {}): SessionRow => ({
  session_id: SESSION,
  task_id: TASK,
  org_id: 'org-1',
  wg_id: WG,
  device_id: DEVICE,
  state: 'working',
  state_reason: null,
  estimate_ms: null,
  created_at: '2026-11-24T10:00:00.000Z',
  first_working_at: '2026-11-24T10:00:01.000Z',
  ...overrides,
});

const entry = (seq: number, header: unknown): EventEntry => ({
  org: 'org-1',
  scopeId: WG,
  scopeSeq: seq,
  receivedAt: null,
  event: { header },
  status: 'pending',
  code: null,
  header: null,
  body: null,
});

afterEach(() => {
  vi.useRealTimers();
});

describe('taskQueue', () => {
  it('parses prompts in event order and withdrawals, leaving out rows it does not know', () => {
    const found = taskQueue({
      task_id: TASK,
      prompts: [
        prompt('e2', { scope_seq: 9, via_agent: true, device_id: null, state: 'waiting_for_device' }),
        prompt('e1', { scope_seq: 4, state: 'withdrawing' }),
        prompt('e3', { state: 'later' }),
        prompt('e4', { state: 5 }),
        prompt('e5', { device_id: 5 }),
        prompt('e6', { scope_seq: 1.5 }),
        prompt('e7', { via_agent: 'yes' }),
        prompt('e8', { session_id: null }),
        prompt('e9', { received_at: null }),
        prompt('e10', { author_id: null }),
        prompt(5 as never),
        'broken',
      ],
      withdrawals: [
        { event_id: 'w1', state: 'too_late', requested_at: '2026-11-24T10:00:00.000Z' },
        { event_id: 'w2', state: 'later', requested_at: '2026-11-24T10:00:00.000Z' },
        { event_id: 'w3', state: 5, requested_at: '2026-11-24T10:00:00.000Z' },
        { event_id: 'w4', state: 'withdrawn' },
        { state: 'withdrawn', requested_at: '2026-11-24T10:00:00.000Z' },
        null,
      ],
    });
    expect(found?.prompts.map((row) => [row.eventId, row.state, row.viaAgent, row.deviceId])).toEqual([
      ['e1', 'withdrawing', false, DEVICE],
      ['e2', 'waiting_for_device', true, null],
    ]);
    expect(found?.prompts[0]).toEqual({
      eventId: 'e1',
      authorId: ALICE,
      viaAgent: false,
      scopeSeq: 4,
      receivedAt: '2026-11-24T10:00:00.000Z',
      sessionId: SESSION,
      deviceId: DEVICE,
      state: 'withdrawing',
    });
    expect(found?.withdrawals).toEqual([{ eventId: 'w1', state: 'too_late', requestedAt: '2026-11-24T10:00:00.000Z' }]);
  });

  it('accepts an answer without withdrawals and refuses malformed answers', () => {
    expect(taskQueue({ prompts: [] })).toEqual({ prompts: [], withdrawals: [] });
    expect(taskQueue({ prompts: 'x' })).toBeNull();
    expect(taskQueue(null)).toBeNull();
  });
});

describe('loadQueue and withdrawPrompt', () => {
  it('loads the queue into the store, refuses a malformed answer, writes nothing after a sign-out', async () => {
    const { store, client, context } = world();
    let answer: () => unknown = () => ({ task_id: TASK, prompts: [prompt('e1')], withdrawals: [] });
    client.on(`GET /v1/tasks/${TASK}/queue`, () => answer());
    expect((await loadQueue(context, TASK)).prompts).toHaveLength(1);
    expect(store.getState().queue.byTask[TASK]!.prompts[0]!.eventId).toBe('e1');
    answer = () => ({ prompts: null });
    await expect(loadQueue(context, TASK)).rejects.toMatchObject({ code: 'unavailable' });
    answer = () => {
      store.reset();
      return { prompts: [] };
    };
    await loadQueue(context, TASK);
    expect(store.getState().queue.byTask).toEqual({});
  });

  it('withdraws a prompt, shows the outcome at once and reads the queue again', async () => {
    const { store, client, context } = world();
    const route = `POST /v1/tasks/${TASK}/prompts/e1/withdraw`;
    let answer: unknown = { state: 'withdrawing' };
    client.on(route, () => answer);
    // without a loaded queue and with a failing read nothing is shown, the answer is returned
    expect(await withdrawPrompt(context, TASK, 'e1')).toBe('withdrawing');
    expect(client.of(route)[0]!.body).toEqual({});
    expect(store.getState().queue.byTask[TASK]).toBeUndefined();
    putQueue(store, TASK, {
      prompts: taskQueue({ prompts: [prompt('e1'), prompt('e2', { scope_seq: 6 })] })!.prompts,
      withdrawals: [{ eventId: 'e1', state: 'too_late', requestedAt: 'x' }],
    });
    expect(await withdrawPrompt(context, TASK, 'e1')).toBe('withdrawing');
    let queue = store.getState().queue.byTask[TASK]!;
    expect(queue.prompts.map((row) => [row.eventId, row.state])).toEqual([
      ['e1', 'withdrawing'],
      ['e2', 'waiting'],
    ]);
    expect(queue.withdrawals).toEqual([
      { eventId: 'e1', state: 'withdrawing', requestedAt: '2026-11-24T11:00:00.000Z' },
    ]);
    expect(selectWithdrawal('e1')(store.getState())).toBe('withdrawing');
    expect(selectWithdrawal('e2')(store.getState())).toBeNull();
    answer = { state: 'withdrawn' };
    expect(await withdrawPrompt(context, TASK, 'e1')).toBe('withdrawn');
    queue = store.getState().queue.byTask[TASK]!;
    expect(queue.prompts.map((row) => row.eventId)).toEqual(['e2']);
    expect(queue.withdrawals.map((row) => row.state)).toEqual(['withdrawn']);
    // the read after the withdrawal replaces the shown outcome
    client.on(`GET /v1/tasks/${TASK}/queue`, () => ({ prompts: [], withdrawals: [] }));
    await withdrawPrompt(context, TASK, 'e1');
    expect(store.getState().queue.byTask[TASK]).toEqual({ prompts: [], withdrawals: [] });
    for (const wrong of [{ state: 'later' }, null]) {
      answer = wrong;
      await expect(withdrawPrompt(context, TASK, 'e1')).rejects.toMatchObject({ code: 'unavailable' });
    }
    client.on(route, () => {
      throw new HubError('peer_outdated', 409);
    });
    await expect(withdrawPrompt(context, TASK, 'e1')).rejects.toMatchObject({ code: 'peer_outdated' });
  });
});

describe('queueInputs', () => {
  it('follows the task row, its session entries and the number of its prompt events', () => {
    const { store } = world();
    const read = () => queueInputs(store.getState(), TASK);
    expect(read()).toEqual({ row: undefined, sessions: [], prompts: 0 });
    putTasks(store, [task()]);
    const bare = read();
    expect(bare.row?.task_id).toBe(TASK);
    expect(sameInputs(bare, read())).toBe(true);
    store.update('events', () => ({
      [WG]: {
        entries: new Map([
          [1, entry(1, { kind: 'prompt', task: TASK })],
          [2, entry(2, { kind: 'prompt', task: 'other' })],
          [3, entry(3, { kind: 'session.output', task: TASK })],
          [4, { ...entry(4, null), event: null }],
          [5, entry(5, null)],
        ]),
      },
    }));
    const one = read();
    expect(one.prompts).toBe(1);
    expect(sameInputs(bare, one)).toBe(false);
    putSession(store, session(), [], 0);
    putSession(store, session({ session_id: 'elsewhere', task_id: 'other' }), [], 0);
    const first = read();
    expect(first.sessions.map((item) => item.row.session_id)).toEqual([SESSION]);
    expect(sameInputs(one, first)).toBe(false);
    // a session frame with an unchanged row replaces the entry: a new read
    putSession(store, session(), [], 0);
    expect(sameInputs(first, read())).toBe(false);
    putSession(store, session({ session_id: 'child' }), [], 0);
    const two = read();
    expect(two.sessions).toHaveLength(2);
    expect(sameInputs(two, { ...two, sessions: [two.sessions[0]!, first.sessions[0]!] })).toBe(false);
    expect(sameInputs(two, { ...two, row: task() })).toBe(false);
    store.update('events', () => ({ [WG]: { entries: new Map() } }));
    expect(read().prompts).toBe(0);
    expect(queueInputs(store.getState(), 'other').row).toBeUndefined();
  });
});

describe('watchQueue', () => {
  it('reads at once, debounced after changes, one read at a time, polls while prompts wait', async () => {
    vi.useFakeTimers();
    const { store, client, context } = world();
    putTasks(store, [task()]);
    let release: (() => void) | null = null;
    let prompts: unknown[] = [];
    client.on(`GET /v1/tasks/${TASK}/queue`, async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return { prompts, withdrawals: [] };
    });
    const reads = () => client.of(`GET /v1/tasks/${TASK}/queue`).length;
    const finish = async () => {
      const open = release as (() => void) | null;
      release = null;
      open?.();
      await vi.advanceTimersByTimeAsync(0);
    };
    const stop = watchQueue(context, TASK);
    expect(reads()).toBe(1);
    // two changes within the pause: one read after the pause, queued behind the running one
    putTasks(store, [task({ state: 'idle', state_at: '2026-11-24T10:01:00.000Z' })]);
    await vi.advanceTimersByTimeAsync(QUEUE_DEBOUNCE_MS / 2);
    putTasks(store, [task({ state: 'running', state_at: '2026-11-24T10:02:00.000Z' })]);
    await vi.advanceTimersByTimeAsync(QUEUE_DEBOUNCE_MS);
    expect(reads()).toBe(1);
    await finish();
    expect(reads()).toBe(2);
    await finish();
    expect(reads()).toBe(2);
    // the poll reads only while prompts wait
    await vi.advanceTimersByTimeAsync(QUEUE_POLL_MS);
    expect(reads()).toBe(2);
    prompts = [prompt('e1')];
    putTasks(store, [task({ state: 'idle', state_at: '2026-11-24T10:03:00.000Z' })]);
    await vi.advanceTimersByTimeAsync(QUEUE_DEBOUNCE_MS);
    await finish();
    expect(store.getState().queue.byTask[TASK]!.prompts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(QUEUE_POLL_MS);
    expect(reads()).toBe(4);
    await finish();
    // a pending change is dropped when the watch stops
    putTasks(store, [task({ state: 'running', state_at: '2026-11-24T10:04:00.000Z' })]);
    stop();
    await vi.advanceTimersByTimeAsync(QUEUE_POLL_MS);
    expect(reads()).toBe(4);
  });

  it('reports failed reads while watching, not after the stop', async () => {
    vi.useFakeTimers();
    const { store, client, context } = world();
    putTasks(store, [task()]);
    const onError = vi.fn();
    let fail: (() => void) | null = null;
    client.on(`GET /v1/tasks/${TASK}/queue`, () => {
      throw new HubError('not_found', 404);
    });
    const stop = watchQueue(context, TASK, { onError, debounceMs: 10, pollMs: 50 });
    await vi.advanceTimersByTimeAsync(0);
    expect(onError).toHaveBeenCalledWith('not_found');
    // the poll reads nothing while no queue was ever loaded
    await vi.advanceTimersByTimeAsync(50);
    expect(client.of(`GET /v1/tasks/${TASK}/queue`)).toHaveLength(1);
    client.on(`GET /v1/tasks/${TASK}/queue`, () => new Promise((_, reject) => (fail = () => reject(new Error('x')))));
    putTasks(store, [task({ state: 'idle', state_at: '2026-11-24T10:01:00.000Z' })]);
    await vi.advanceTimersByTimeAsync(10);
    stop();
    (fail as (() => void) | null)?.();
    await vi.advanceTimersByTimeAsync(0);
    expect(onError).toHaveBeenCalledTimes(1);
    // without an error handler a failure is quiet
    client.on(`GET /v1/tasks/${TASK}/queue`, () => {
      throw new HubError('not_found', 404);
    });
    const quiet = watchQueue(context, TASK);
    await vi.advanceTimersByTimeAsync(0);
    quiet();
  });
});
