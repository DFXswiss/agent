// U-WEB-attention (slice `attention`, KOR-39): record validation, the order of the groups, stale records, pages with
// paging and the refresh after a reconnect, placement of changed records between the groups, the counters, and the
// live `attention` frames applied in batches (at most one store update per frame, nothing after a sign-out).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import { createStore, type Store } from '../../../src/core/store';
import {
  attentionGroup,
  type AttentionRow,
  before,
  type Category,
  FRAME_MS,
  flushAttention,
  frames,
  groupFailed,
  type GroupFilter,
  groupKey,
  groupLoading,
  groupPage,
  isAttentionRow,
  isCategory,
  isSummary,
  matches,
  putSummary,
  queueAttention,
  staleRow,
} from '../../../src/core/store/slices/attention';
import { setActiveOrg } from '../../../src/core/store/slices/session';
import { at, record } from './rows';

const ids = (store: Store, category: Category, org = 'o') =>
  attentionGroup(store.getState().attention, org, category).ids;

function page(store: Store, category: Category, rows: AttentionRow[], nextBefore: string | null = null, first = true) {
  groupPage(store, { org: 'o', category, rows, nextBefore, first });
}

describe('validation', () => {
  it('accepts well-formed records and counters only', () => {
    expect(isAttentionRow(record('t'))).toBe(true);
    expect(isAttentionRow(record('t', { ref_kind: null, ref_id: null }))).toBe(true);
    expect(isAttentionRow(record('t', { ref_kind: 'session' }))).toBe(true);
    expect(isAttentionRow(record('t', { reason: 'quota_choice', wait_until: at(5) }))).toBe(true);
    expect(isAttentionRow(record('t', { reason: 'quota_wait', category: 'working', wait_until: null }))).toBe(true);
    for (const bad of [
      { task_id: '' },
      { category: 'later' },
      { reason: 1 },
      { ref_kind: 'device' },
      { ref_id: '' },
      { since: '2026-11-21' },
      { since: 5 },
      { seq: -1 },
      { subagents_total: 1.5 },
      { subagents_active: null },
      { updated_at: 'x' },
      { wait_until: 'soon' },
      { wait_until: 5 },
    ])
      expect(isAttentionRow({ ...record('t'), ...bad })).toBe(false);
    expect(isAttentionRow(null)).toBe(false);
    expect(isCategory('done')).toBe(true);
    expect(isCategory('idle')).toBe(false);
    const summary = { needs_decision: 1, needs_decision_mine: 0, blocked: 2, working: 3, done_since: 4 };
    expect(isSummary(summary)).toBe(true);
    expect(isSummary({ ...summary, working: -1 })).toBe(false);
    expect(isSummary([])).toBe(false);
  });
});

describe('order', () => {
  it('puts the longest waiting decision first and the newest entry of the other groups first', () => {
    const early = record('b', { since: at(1) });
    const late = record('a', { since: at(2) });
    expect(before('needs_decision', early, late)).toBe(true);
    expect(before('blocked', early, late)).toBe(false);
    expect(before('blocked', late, early)).toBe(true);
    // equal times: the task ID decides (ascending for decisions, descending otherwise)
    const tie = record('c', { since: at(1) });
    expect(before('needs_decision', early, tie)).toBe(true);
    expect(before('working', early, tie)).toBe(false);
  });

  it('knows stale records by the update time, then the counter', () => {
    const known = record('t', { updated_at: at(5), seq: 3 });
    expect(staleRow(undefined, known)).toBe(false);
    expect(staleRow(known, record('t', { updated_at: at(4), seq: 9 }))).toBe(true);
    expect(staleRow(known, record('t', { updated_at: at(5), seq: 2 }))).toBe(true);
    expect(staleRow(known, record('t', { updated_at: at(5), seq: 3 }))).toBe(false);
    expect(staleRow(known, record('t', { updated_at: at(6), seq: 1 }))).toBe(false);
  });
});

describe('groups', () => {
  it('are idle and empty until loaded (the same object every time); load, fail and keep counters', () => {
    const store = createStore();
    const idle = attentionGroup(store.getState().attention, 'o', 'blocked');
    expect(idle).toMatchObject({ status: 'idle', ids: [], nextBefore: null, code: null });
    expect(attentionGroup(store.getState().attention, 'o', 'blocked')).toBe(idle);
    groupLoading(store, 'o', 'blocked');
    expect(attentionGroup(store.getState().attention, 'o', 'blocked').status).toBe('loading');
    groupFailed(store, 'o', 'blocked', 'unavailable');
    expect(attentionGroup(store.getState().attention, 'o', 'blocked')).toMatchObject({
      status: 'error',
      code: 'unavailable',
    });
    groupFailed(store, 'o', 'working', 'forbidden');
    expect(attentionGroup(store.getState().attention, 'o', 'working').code).toBe('forbidden');
    groupLoading(store, 'o', 'blocked');
    expect(attentionGroup(store.getState().attention, 'o', 'blocked').code).toBeNull();
    const summary = { needs_decision: 1, needs_decision_mine: 1, blocked: 0, working: 0, done_since: 0 };
    putSummary(store, 'o', summary);
    expect(store.getState().attention.summaries.o).toEqual(summary);
  });

  it('page in the hub order, skip records of another category and continue with further pages', () => {
    const store = createStore();
    page(
      store,
      'working',
      [
        record('a', { category: 'working', since: at(3) }),
        record('x', { category: 'done' }),
        record('b', { category: 'working', since: at(2) }),
      ],
      'cursor',
    );
    expect(ids(store, 'working')).toEqual(['a', 'b']);
    expect(attentionGroup(store.getState().attention, 'o', 'working')).toMatchObject({
      status: 'ready',
      nextBefore: 'cursor',
    });
    page(store, 'working', [record('c', { category: 'working', since: at(1) })], null, false);
    expect(ids(store, 'working')).toEqual(['a', 'b', 'c']);
    expect(attentionGroup(store.getState().attention, 'o', 'working').nextBefore).toBeNull();
    // the first page again starts the group over
    page(store, 'working', [record('c', { category: 'working', since: at(1) })]);
    expect(ids(store, 'working')).toEqual(['c']);
  });

  it('refresh the first page after a reconnect and keep the further pages loaded before', () => {
    const store = createStore();
    const rows = [5, 4, 3, 2].map((minute) => record(`t${minute}`, { category: 'blocked', since: at(minute) }));
    page(store, 'blocked', rows.slice(0, 2), 'c1');
    page(store, 'blocked', rows.slice(2), 'c2', false);
    const fresh = record('t6', { category: 'blocked', since: at(6) });
    groupPage(store, {
      org: 'o',
      category: 'blocked',
      rows: [fresh, rows[0]!],
      nextBefore: 'c0',
      first: true,
      refresh: true,
    });
    expect(ids(store, 'blocked')).toEqual(['t6', 't5', 't4', 't3', 't2']);
    // the paging position stays where the loaded pages ended
    expect(attentionGroup(store.getState().attention, 'o', 'blocked').nextBefore).toBe('c2');
    // a refresh that reaches the end, or that brings no record, keeps nothing older
    groupPage(store, { org: 'o', category: 'blocked', rows: [fresh], nextBefore: null, first: true, refresh: true });
    expect(ids(store, 'blocked')).toEqual(['t6']);
    groupPage(store, { org: 'o', category: 'blocked', rows: [], nextBefore: 'c9', first: true, refresh: true });
    expect(ids(store, 'blocked')).toEqual([]);
    expect(attentionGroup(store.getState().attention, 'o', 'blocked').nextBefore).toBe('c9');
    // a refresh whose page covers everything loaded keeps the page's cursor
    page(store, 'blocked', [fresh], 'c1');
    groupPage(store, { org: 'o', category: 'blocked', rows: [fresh], nextBefore: 'cx', first: true, refresh: true });
    expect(attentionGroup(store.getState().attention, 'o', 'blocked').nextBefore).toBe('cx');
  });

  it('move a changed record between the loaded groups of its org', () => {
    const store = createStore();
    setActiveOrg(store, 'o');
    const change = (row: AttentionRow) => {
      queueAttention(store, row);
      flushAttention(store);
    };
    page(store, 'needs_decision', [record('q', { since: at(1) })]);
    page(store, 'working', [record('w', { category: 'working', since: at(2) })], 'more');
    groupPage(store, { org: 'other', category: 'working', rows: [], nextBefore: null, first: true });
    // the answered question runs again: it leaves "Needs your decision" and is newer than the last loaded entry
    change(record('q', { category: 'working', reason: 'working', since: at(3), updated_at: at(3), seq: 2 }));
    expect(ids(store, 'needs_decision')).toEqual([]);
    expect(ids(store, 'working')).toEqual(['q', 'w']);
    expect(ids(store, 'working', 'other')).toEqual([]);
    // an entry older than the last loaded one of a group with a further page waits for that page
    change(record('old', { category: 'working', since: at(0) }));
    expect(ids(store, 'working')).toEqual(['q', 'w']);
    // a stale record changes nothing
    change(record('q', { since: at(1) }));
    expect(ids(store, 'needs_decision')).toEqual([]);
    expect(ids(store, 'working')).toEqual(['q', 'w']);
    // a page row a newer frame moved to another category meanwhile stays out of the page's group
    page(store, 'needs_decision', [record('q', { since: at(1), updated_at: at(1), seq: 1 })]);
    expect(ids(store, 'needs_decision')).toEqual([]);
  });
});

describe('narrowed groups', () => {
  const MINE: GroupFilter = { owner: 'me', after: null };
  const TODAY: GroupFilter = { owner: null, after: at(2) };
  const narrowed = (store: Store, category: Category, filter: GroupFilter) =>
    attentionGroup(store.getState().attention, 'o', category, filter);

  it('are groups of their own: keyed, idle, loading and failing apart from the whole category', () => {
    const store = createStore();
    expect(groupKey('o', 'blocked')).not.toBe(groupKey('o', 'blocked', MINE));
    expect(groupKey('o', 'done', TODAY)).not.toBe(groupKey('o', 'done', { owner: null, after: at(3) }));
    expect(narrowed(store, 'needs_decision', MINE)).toMatchObject({ filter: MINE, status: 'idle', ids: [] });
    groupLoading(store, 'o', 'needs_decision', MINE);
    expect(narrowed(store, 'needs_decision', MINE).status).toBe('loading');
    expect(attentionGroup(store.getState().attention, 'o', 'needs_decision').status).toBe('idle');
    groupFailed(store, 'o', 'needs_decision', 'forbidden', MINE);
    expect(narrowed(store, 'needs_decision', MINE)).toMatchObject({ status: 'error', code: 'forbidden' });
    expect(attentionGroup(store.getState().attention, 'o', 'needs_decision').code).toBeNull();
  });

  it('match a record by category, known owner and the bound', () => {
    const store = createStore();
    const mine = narrowed(store, 'needs_decision', MINE);
    const today = narrowed(store, 'done', TODAY);
    expect(matches(mine, record('a'), { a: 'me' })).toBe(true);
    expect(matches(mine, record('a'), { a: 'other' })).toBe(false);
    expect(matches(mine, record('a'), {})).toBe(false);
    expect(matches(mine, record('a', { category: 'blocked' }), { a: 'me' })).toBe(false);
    expect(matches(today, record('d', { category: 'done', since: at(2) }), {})).toBe(true);
    expect(matches(today, record('d', { category: 'done', since: at(1) }), {})).toBe(false);
  });

  it('page with the owners of their tasks and take in changed records only when they match', () => {
    const store = createStore();
    setActiveOrg(store, 'o');
    const change = (row: AttentionRow) => {
      queueAttention(store, row);
      flushAttention(store);
    };
    groupPage(store, {
      org: 'o',
      category: 'needs_decision',
      filter: MINE,
      rows: [record('a', { since: at(1) })],
      owners: { a: 'me' },
      nextBefore: null,
      first: true,
    });
    page(store, 'blocked', [record('b', { category: 'blocked' }), record('c', { category: 'blocked' })]);
    groupPage(store, {
      org: 'o',
      category: 'blocked',
      rows: [],
      owners: { b: 'me', c: 'other' },
      nextBefore: null,
      first: false,
    });
    groupPage(store, { org: 'o', category: 'done', filter: TODAY, rows: [], nextBefore: null, first: true });
    expect(narrowed(store, 'needs_decision', MINE).ids).toEqual(['a']);
    expect(store.getState().attention.owners).toEqual({ a: 'me', b: 'me', c: 'other' });
    // a task of the user's that comes to need a decision enters; one of another owner does not
    change(record('b', { since: at(2), updated_at: at(2) }));
    change(record('c', { since: at(3), updated_at: at(3) }));
    change(record('u', { since: at(4), updated_at: at(4) }));
    expect(narrowed(store, 'needs_decision', MINE).ids).toEqual(['a', 'b']);
    expect(ids(store, 'needs_decision')).toEqual([]);
    // a decision answered leaves the narrowed group
    change(record('a', { category: 'working', since: at(5), updated_at: at(5) }));
    expect(narrowed(store, 'needs_decision', MINE).ids).toEqual(['b']);
    // "today": only records finished at or after the bound
    change(record('b', { category: 'done', since: at(6), updated_at: at(6) }));
    change(record('c', { category: 'done', since: at(1), updated_at: at(6) }));
    expect(narrowed(store, 'done', TODAY).ids).toEqual(['b']);
    expect(narrowed(store, 'needs_decision', MINE).ids).toEqual([]);
  });
});

describe('live frames', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('are discovered and applied in batches of one store update per frame', () => {
    expect(frameHandlers().some((set) => set === frames)).toBe(true);
    const store = createStore();
    setActiveOrg(store, 'o');
    page(store, 'needs_decision', []);
    page(store, 'working', [record('w', { category: 'working', since: at(1) })]);
    let updates = 0;
    store.subscribe(() => (updates += 1));
    expect(applyFrame(store, 'attention', record('q1', { since: at(2) }), 0)).toBe(1);
    applyFrame(store, 'attention', record('q2', { since: at(1) }), 0);
    applyFrame(store, 'attention', { task_id: 'broken' }, 0);
    applyFrame(store, 'attention', record('w', { category: 'needs_decision', since: at(3), updated_at: at(3) }), 0);
    expect(updates).toBe(0);
    vi.advanceTimersByTime(FRAME_MS);
    expect(updates).toBe(1);
    expect(ids(store, 'needs_decision')).toEqual(['q2', 'q1', 'w']);
    expect(ids(store, 'working')).toEqual([]);
    expect(store.getState().attention.changes).toBe(1);
    expect(store.getState().attention.orgs.q1).toBe('o');
    // the record's own org wins over the channel's
    setActiveOrg(store, 'p');
    queueAttention(store, record('q1', { category: 'blocked', since: at(4), updated_at: at(4) }));
    flushAttention(store);
    flushAttention(store);
    expect(store.getState().attention.orgs.q1).toBe('o');
    expect(ids(store, 'needs_decision')).toEqual(['q2', 'w']);
    expect(store.getState().attention.changes).toBe(2);
  });

  it('are dropped after a sign-out and without a known org', () => {
    const store = createStore();
    queueAttention(store, record('q'));
    store.reset();
    queueAttention(store, record('r'));
    vi.advanceTimersByTime(FRAME_MS);
    // no org known: the batch counts but stores nothing
    expect(store.getState().attention.rows).toEqual({});
    expect(store.getState().attention.changes).toBe(1);
    const other = createStore();
    queueAttention(other, record('q'));
    other.reset();
    vi.advanceTimersByTime(FRAME_MS);
    expect(other.getState().attention.changes).toBe(0);
  });
});
