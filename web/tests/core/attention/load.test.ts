// U-WEB-attention (loading the situation view, KOR-39): the pages of a group with their task rows (other orgs and
// malformed answers refused, failures recorded, paging, the refresh after a reconnect, nothing after a sign-out), the
// counters since local midnight at most once per second, the row of a task named only by a frame, and the operation
// behind a question.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DONE_DAYS,
  ensureTask,
  groupTarget,
  loadGroup,
  loadGroupMore,
  loadQuestion,
  loadSummary,
  localMidnight,
  PAGE_SIZE,
  refreshSummary,
  SUMMARY_MS,
} from '../../../src/core/attention/load';
import { formatTime } from '../../../src/core/encoding/time';
import { HubError } from '../../../src/core/net/signedFetch';
import { attentionGroup, type Category } from '../../../src/core/store/slices/attention';
import { T0 } from '../log/world';
import { taskRow, type TaskWorld, taskWorld } from '../tasks/world';
import { at, record } from './rows';

const item = (t: TaskWorld, task: string, overrides: Record<string, unknown> = {}) => ({
  task: taskRow(t, { task_id: task }),
  attention: record(task, overrides),
});

const group = (t: TaskWorld, category: Category = 'needs_decision') =>
  attentionGroup(t.store.getState().attention, t.org, category);

describe('targets and times', () => {
  it('asks for one category, the last 7 days of finished tasks, the cursor and the page size', () => {
    expect(groupTarget('o 1', 'blocked', null, T0)).toBe(
      `/v1/orgs/o%201/attention?category=blocked&limit=${PAGE_SIZE}`,
    );
    const since = encodeURIComponent(formatTime(T0 - DONE_DAYS * 86_400_000));
    expect(groupTarget('o', 'done', 'c', T0)).toBe(
      `/v1/orgs/o/attention?category=done&since=${since}&before=c&limit=50`,
    );
  });

  it('starts "today" at local midnight', () => {
    const midnight = localMidnight(T0);
    const date = new Date(midnight);
    expect([date.getHours(), date.getMinutes(), date.getSeconds(), date.getMilliseconds()]).toEqual([0, 0, 0, 0]);
    expect(T0 - midnight).toBeLessThan(86_400_000);
    expect(T0 - midnight).toBeGreaterThanOrEqual(0);
  });
});

describe('groups', () => {
  it('load the first and the next page with their task rows; other orgs are refused', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/attention`, (_body, target) =>
      target.includes('before=')
        ? { items: [item(t, 'c', { since: at(3) })], next_before: null }
        : {
            items: [
              item(t, 'a', { since: at(1) }),
              { ...item(t, 'x'), task: taskRow(t, { task_id: 'x', org_id: 'other' }) },
            ],
            next_before: 'cursor',
          },
    );
    expect(await loadGroup(t.context, t.org, 'needs_decision')).toBe(1);
    expect(group(t)).toMatchObject({ ids: ['a'], nextBefore: 'cursor', status: 'ready' });
    expect(t.store.getState().tasks.tasks.a).toBeDefined();
    expect(t.store.getState().tasks.tasks.x).toBeUndefined();
    expect(await loadGroupMore(t.context, t.org, 'needs_decision')).toBe(1);
    expect(group(t).ids).toEqual(['a', 'c']);
    expect(t.client.calls.at(-1)!.target).toContain('before=cursor');
    // no further page: nothing is requested
    expect(await loadGroupMore(t.context, t.org, 'needs_decision')).toBe(0);
    expect(t.client.calls).toHaveLength(2);
  });

  it('leave a loading group alone, record failures and refuse malformed answers', async () => {
    const t = await taskWorld();
    let answer: unknown = { items: [], next_before: null };
    let release: () => void = () => undefined;
    t.client.on(`GET /v1/orgs/${t.org}/attention`, () =>
      answer instanceof Error
        ? Promise.reject(answer)
        : new Promise((resolve) => {
            release = () => resolve(answer);
          }),
    );
    const first = loadGroup(t.context, t.org, 'blocked');
    expect(group(t, 'blocked').status).toBe('loading');
    expect(await loadGroup(t.context, t.org, 'blocked')).toBe(0);
    release();
    expect(await first).toBe(0);
    expect(group(t, 'blocked')).toMatchObject({ status: 'ready', ids: [] });
    answer = new HubError('forbidden', 403);
    await loadGroup(t.context, t.org, 'blocked');
    expect(group(t, 'blocked')).toMatchObject({ status: 'error', code: 'forbidden' });
    for (const bad of [
      null,
      { items: 'x', next_before: null },
      { items: [{ task: {}, attention: record('a') }], next_before: null },
      { items: [{ task: taskRow(t, { task_id: 'b' }), attention: record('a') }], next_before: null },
      { items: [], next_before: 1 },
    ]) {
      answer = bad;
      const load = loadGroup(t.context, t.org, 'working');
      release();
      await load;
      expect(group(t, 'working')).toMatchObject({ status: 'error', code: 'unavailable' });
    }
  });

  it('refresh after a reconnect without losing the loaded state, and write nothing after a sign-out', async () => {
    const t = await taskWorld();
    let fail = false;
    t.client.on(`GET /v1/orgs/${t.org}/attention`, () => {
      if (fail) throw new HubError('unavailable', 503);
      return { items: [item(t, 'a', { category: 'working', since: at(2) })], next_before: null };
    });
    await loadGroup(t.context, t.org, 'working', false, true);
    expect(group(t, 'working').ids).toEqual(['a']);
    fail = true;
    await loadGroup(t.context, t.org, 'working', false, true);
    expect(group(t, 'working')).toMatchObject({ status: 'ready', ids: ['a'] });
    fail = false;
    const load = loadGroup(t.context, t.org, 'done');
    t.store.reset();
    expect(await load).toBe(0);
    expect(group(t, 'done').status).toBe('idle');
    fail = true;
    const failing = loadGroup(t.context, t.org, 'done');
    t.store.reset();
    await failing;
    expect(group(t, 'done').status).toBe('idle');
  });
});

describe('counters', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const summary = { needs_decision: 2, needs_decision_mine: 1, blocked: 0, working: 3, done_since: 4 };

  it('load since local midnight; failures and malformed answers keep the old ones', async () => {
    const t = await taskWorld();
    let answer: unknown = summary;
    t.client.on(`GET /v1/orgs/${t.org}/attention/summary`, () => {
      if (answer instanceof Error) throw answer;
      return answer;
    });
    await loadSummary(t.context, t.org);
    expect(t.store.getState().attention.summaries[t.org]).toEqual(summary);
    expect(t.client.calls[0]!.target).toBe(
      `/v1/orgs/${t.org}/attention/summary?since=${encodeURIComponent(formatTime(localMidnight(T0)))}`,
    );
    answer = { ...summary, blocked: -1 };
    await loadSummary(t.context, t.org);
    answer = new HubError('unavailable', 503);
    await loadSummary(t.context, t.org);
    expect(t.store.getState().attention.summaries[t.org]).toEqual(summary);
    // an answer after a sign-out writes nothing
    answer = summary;
    const load = loadSummary(t.context, t.org);
    t.store.reset();
    await load;
    expect(t.store.getState().attention.summaries).toEqual({});
  });

  it('are asked at most once per second; a burst costs one more request at its end', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/attention/summary`, () => summary);
    const calls = () => t.client.of(`GET /v1/orgs/${t.org}/attention/summary`).length;
    refreshSummary(t.context, t.org);
    expect(calls()).toBe(1);
    t.clock.now += 100;
    refreshSummary(t.context, t.org);
    refreshSummary(t.context, t.org);
    expect(calls()).toBe(1);
    t.clock.now += SUMMARY_MS - 100;
    vi.advanceTimersByTime(SUMMARY_MS - 100);
    expect(calls()).toBe(2);
    t.clock.now += SUMMARY_MS;
    refreshSummary(t.context, t.org);
    expect(calls()).toBe(3);
  });
});

describe('task rows and questions', () => {
  it('load a task named only by a frame once; a failure is forgotten', async () => {
    const t = await taskWorld();
    let fail = true;
    t.client.on('GET /v1/tasks/a', () => {
      if (fail) throw new HubError('unavailable', 503);
      return { task: taskRow(t, { task_id: 'a' }), sessions: [] };
    });
    expect(await ensureTask(t.context, 'a')).toBeNull();
    fail = false;
    const first = ensureTask(t.context, 'a');
    expect(ensureTask(t.context, 'a')).toBe(first);
    await first;
    expect(t.store.getState().tasks.tasks.a).toBeDefined();
    expect(t.client.of('GET /v1/tasks/a')).toHaveLength(2);
  });

  it('load the operation and its sealed parameters', async () => {
    const t = await taskWorld();
    const op = '0190f000-0000-7000-8000-0000000000b1';
    t.client.on(`GET /v1/operations/${op}`, () => ({
      operation: {
        operation_id: op,
        org_id: t.org,
        wg_id: t.wg,
        task_id: 'a',
        session_id: 's',
        device_id: 'd',
        actor_id: 'd',
        type: 'human.question',
        state: 'succeeded',
        error_code: null,
        retryable: false,
        duplicate_of: null,
        attempt: 1,
        params_event_id: null,
        result_event_id: null,
        created_at: at(0),
        decided_at: null,
        started_at: null,
        finished_at: null,
      },
      transitions: [],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    await loadQuestion(t.context, taskRow(t, { task_id: 'a' }) as never, op);
    expect(t.store.getState().operations.rows[op]!.type).toBe('human.question');
    expect(t.client.of(`GET /v1/scopes/${t.wg}/events`)[0]!.target).toContain(`task=a&ref=${op}`);
  });
});
