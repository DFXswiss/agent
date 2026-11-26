// U-WEB-ui-tasks (list filters, ADR-100 section 5): the list key of the hub filters (the state alone keeps the key of
// before), its inverse, the query parameters, and which rows a filtered list holds.
import { describe, expect, it } from 'vitest';
import type { TaskRow } from '../../../src/core/store/slices/tasks';
import { filterKey, filterParams, matchesFilter, NONE, parseFilter, rowStatus } from '../../../src/core/tasks/filters';

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
