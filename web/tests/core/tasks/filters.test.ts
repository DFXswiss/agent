// U-WEB-ui-tasks (list filters, ADR-100 section 5): the list key of the hub filters (the state alone keeps the key of
// before), its inverse, the query parameters, and which rows a filtered list holds.
import { describe, expect, it } from 'vitest';
import type { TaskRow } from '../../../src/core/store/slices/tasks';
import {
  byOverview,
  comesAfter,
  filterKey,
  filterParams,
  matchesFilter,
  NONE,
  orderOf,
  OVERVIEWS,
  parseFilter,
  rowStatus,
} from '../../../src/core/tasks/filters';

const base = {
  task_id: 't',
  org_id: 'o',
  wg_id: 'w',
  owner_id: 'u',
  state: 'idle',
  state_reason: null,
  created_at: '2026-11-21T12:00:00.000Z',
  state_at: '2026-11-21T12:00:00.000Z',
  last_activity_at: '2026-11-21T12:00:00.000Z',
  closed_at: null,
  main_session_id: null,
  title_event_id: null,
} as TaskRow;

describe('filter keys', () => {
  it('keys nothing as null and the state alone as the state', () => {
    expect(filterKey({})).toBeNull();
    expect(filterKey({ state: null, status: '' })).toBeNull();
    expect(filterKey({ state: 'idle' })).toBe('idle');
    expect(parseFilter(null)).toEqual({});
    expect(parseFilter('idle')).toEqual({ state: 'idle' });
  });

  it('keys the ticket filters in a fixed order and reads them back', () => {
    const key = filterKey({ topic: NONE, state: 'idle', assignee: 'u1', urgency: 'high', status: 'todo' });
    expect(key).toBe('state=idle&status=todo&urgency=high&assignee=u1&topic=none');
    expect(parseFilter(key)).toEqual({ state: 'idle', status: 'todo', urgency: 'high', assignee: 'u1', topic: NONE });
    expect(filterKey({ status: 'todo' })).toBe('status=todo');
    expect(parseFilter('status=todo')).toEqual({ status: 'todo' });
    const params = new URLSearchParams();
    filterParams('status=todo&topic=none', params);
    expect(params.toString()).toBe('status=todo&topic=none');
    filterParams(null, params);
    expect(params.toString()).toBe('status=todo&topic=none');
  });
});

describe('the order of a list (ADR-101 section 2)', () => {
  it('keys the order last and sends it only when asked', () => {
    const key = filterKey({ status: 'todo', order: 'urgency' });
    expect(key).toBe('status=todo&order=urgency');
    expect(orderOf(key)).toBe('urgency');
    expect(orderOf('order=created')).toBe('created');
    expect(orderOf('order=bogus')).toBe('activity');
    expect(orderOf(null)).toBe('activity');
    expect(orderOf('idle')).toBe('activity');
    const all = new URLSearchParams();
    filterParams(key, all);
    expect(all.toString()).toBe('status=todo&order=urgency');
    const counted = new URLSearchParams();
    filterParams(key, counted, false);
    expect(counted.toString()).toBe('status=todo');
  });

  it('orders rows by urgency, creation or activity, every order descending with the task ID last', () => {
    const row = (task: string, urgency: string | undefined, created: number, activity: number) =>
      ({
        ...base,
        task_id: task,
        ...(urgency !== undefined && { urgency }),
        created_at: `2026-11-21T12:0${created}:00.000Z`,
        last_activity_at: `2026-11-21T12:0${activity}:00.000Z`,
      }) as TaskRow;
    const urgent = row('a', 'urgent', 1, 1);
    const plain = row('b', undefined, 2, 3);
    const low = row('c', 'low', 3, 2);
    const twin = row('d', undefined, 2, 3);
    expect(comesAfter('urgency', plain, urgent)).toBe(true);
    expect(comesAfter('urgency', urgent, plain)).toBe(false);
    expect(comesAfter('urgency', low, plain)).toBe(true);
    expect(comesAfter('urgency', { ...plain, urgency: 'bogus' }, low)).toBe(false);
    expect(comesAfter('created', plain, low)).toBe(true);
    expect(comesAfter('activity', plain, low)).toBe(false);
    expect(comesAfter('activity', plain, twin)).toBe(true);
    expect(comesAfter('activity', twin, plain)).toBe(false);
    expect(comesAfter('urgency', plain, twin)).toBe(true);
  });
});

describe('the overview of the open tasks (ADR-101)', () => {
  it('keys the overview before the order and says which lists are filtered by it', () => {
    const key = filterKey({ overview: 'no_agent', order: 'urgency', status: 'todo' });
    expect(key).toBe('status=todo&overview=no_agent&order=urgency');
    expect(parseFilter(key).overview).toBe('no_agent');
    expect(byOverview(key)).toBe(true);
    expect(byOverview('status=todo')).toBe(false);
    expect(byOverview(null)).toBe(false);
    expect(OVERVIEWS).toEqual(['working', 'waiting', 'no_agent']);
  });
});

describe('rows of a filtered list', () => {
  it('derives the status of a row of an older hub', () => {
    expect(rowStatus({ ...base, status: 'todo' })).toBe('todo');
    expect(rowStatus({ ...base, status: 'inProgress' })).toBe('inProgress');
    expect(rowStatus({ ...base, status: 'completed' })).toBe('completed');
    expect(rowStatus(base)).toBe('inProgress');
    expect(rowStatus({ ...base, state: 'closed' })).toBe('completed');
  });

  it('matches every filter', () => {
    const row = { ...base, status: 'todo', urgency: 'high', assignee_id: 'u1', topic_id: null };
    expect(matchesFilter(null, row)).toBe(true);
    expect(matchesFilter('idle', row)).toBe(true);
    expect(matchesFilter('closed', row)).toBe(false);
    expect(matchesFilter('status=todo&urgency=high&assignee=u1&topic=none', row)).toBe(true);
    expect(matchesFilter('status=completed', row)).toBe(false);
    expect(matchesFilter('urgency=low', row)).toBe(false);
    expect(matchesFilter('urgency=normal', base)).toBe(true);
    expect(matchesFilter('assignee=u2', row)).toBe(false);
    expect(matchesFilter('assignee=none', row)).toBe(false);
    expect(matchesFilter('assignee=none', base)).toBe(true);
    expect(matchesFilter('topic=c1', row)).toBe(false);
    expect(matchesFilter('topic=c1', { ...row, topic_id: 'c1' })).toBe(true);
  });
});
