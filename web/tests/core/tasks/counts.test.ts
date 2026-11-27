// U-WEB-ui-tasks (dashboard counters, ADR-103 section 1): the request of one organization's counters (the hub
// filters without the order, one workgroup), the check of the answer (with the start page's three groups of the open
// tasks), the sum over organizations and what the counters depend on among the loaded rows.
import { describe, expect, it } from 'vitest';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import {
  countsTarget,
  loadCounts,
  NO_COUNTS,
  selectCountsSignature,
  sumCounts,
  type TaskCounts,
} from '../../../src/core/tasks/counts';

const ANSWER = {
  by_status: { todo: 2, inProgress: 1, completed: 0 },
  by_urgency: { low: 0, normal: 2, high: 0, urgent: 1 },
  by_overview: { working: 1, waiting: 0, no_agent: 2 },
  total: 3,
};

const client = (answer: unknown) => ({ client: { request: async () => answer } }) as never;

describe('counters', () => {
  it('asks for the hub filters of the list without its order', () => {
    expect(countsTarget('o 1', null, null)).toBe('/v1/orgs/o%201/tasks/counts');
    expect(countsTarget('o', 'status=todo&order=urgency', 'w')).toBe('/v1/orgs/o/tasks/counts?status=todo&wg=w');
    expect(countsTarget('o', 'idle', null)).toBe('/v1/orgs/o/tasks/counts?state=idle');
    expect(countsTarget('o', 'overview=no_agent', null)).toBe('/v1/orgs/o/tasks/counts?overview=no_agent');
  });

  it('reads an answer and refuses one of another form', async () => {
    expect(await loadCounts(client(ANSWER), 'o', null, null)).toEqual({
      byStatus: ANSWER.by_status,
      byUrgency: ANSWER.by_urgency,
      byOverview: ANSWER.by_overview,
      total: 3,
    });
    for (const bad of [
      null,
      [],
      { ...ANSWER, total: -1 },
      { ...ANSWER, total: 1.5 },
      { ...ANSWER, by_status: { todo: 1 } },
      { ...ANSWER, by_urgency: 'x' },
      { ...ANSWER, by_overview: { working: 1 } },
      { by_status: ANSWER.by_status, by_urgency: ANSWER.by_urgency, by_overview: ANSWER.by_overview },
    ]) {
      await expect(loadCounts(client(bad), 'o', null, null)).rejects.toBeInstanceOf(HubError);
    }
  });

  it('sums the organizations', () => {
    const one: TaskCounts = {
      byStatus: { todo: 1, inProgress: 2, completed: 3 },
      byUrgency: { low: 1, normal: 2, high: 2, urgent: 1 },
      byOverview: { working: 1, waiting: 1, no_agent: 1 },
      total: 6,
    };
    expect(sumCounts([])).toEqual(NO_COUNTS);
    expect(sumCounts([one, one])).toEqual({
      byStatus: { todo: 2, inProgress: 4, completed: 6 },
      byUrgency: { low: 2, normal: 4, high: 4, urgent: 2 },
      byOverview: { working: 2, waiting: 2, no_agent: 2 },
      total: 12,
    });
  });

  it('changes what it depends on only when a row of the workgroups appears or changes state, status or urgency', () => {
    const store = createStore();
    const row = (task: string, wg: string, extra: Partial<TaskRow> = {}) =>
      ({
        task_id: task,
        org_id: 'o',
        wg_id: wg,
        owner_id: 'u',
        state: 'idle',
        state_reason: null,
        created_at: '2026-11-21T12:00:00.000Z',
        state_at: '2026-11-21T12:00:00.000Z',
        last_activity_at: '2026-11-21T12:00:00.000Z',
        closed_at: null,
        main_session_id: null,
        title_event_id: null,
        ...extra,
      }) as TaskRow;
    const select = selectCountsSignature('w1,w2');
    expect(select(store.getState())).toBe('');
    expect(selectCountsSignature('')(store.getState())).toBe('');
    putTasks(store, [row('b', 'w1', { status: 'todo' }), row('a', 'w2', { urgency: 'high' }), row('c', 'w3')]);
    const before = select(store.getState());
    expect(before).toBe('a:idle:inProgress:high,b:idle:todo:normal');
    putTasks(store, [row('b', 'w1', { status: 'todo', last_activity_at: '2026-11-21T13:00:00.000Z' })]);
    expect(select(store.getState())).toBe(before);
    putTasks(store, [row('b', 'w1', { status: 'completed', last_activity_at: '2026-11-21T14:00:00.000Z' })]);
    expect(select(store.getState())).toBe('a:idle:inProgress:high,b:idle:completed:normal');
    putTasks(store, [
      row('a', 'w2', { urgency: 'high', state: 'working', last_activity_at: '2026-11-21T15:00:00.000Z' }),
    ]);
    expect(select(store.getState())).toBe('a:working:inProgress:high,b:idle:completed:normal');
  });
});
