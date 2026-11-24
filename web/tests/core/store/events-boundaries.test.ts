// Eviction without a silent gap (ADR-086 section 1): the oldest events go first, every paged list that held an evicted
// or dropped event at or above its boundary raises the boundary, and every mounted list keeps what it shows until
// twice the bound – the newest page a view just opened as much as pages read back – and is never dropped from.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import { show } from '../../../src/core/store/focus';
import { dropEvents, type EventEntry, MAX_EVENTS, putEvents } from '../../../src/core/store/slices/events';
import { boundaryOf, setPage } from '../../../src/core/store/slices/pages';

function entry(seq: number, task = 't', kind = 'session.output'): EventEntry {
  return {
    org: 'o',
    scopeId: 'wg',
    scopeSeq: seq,
    receivedAt: null,
    event: { header: { kind, task } },
    status: 'pending',
    code: null,
    header: null,
    body: null,
  };
}

const range = (from: number, count: number, task = 't') =>
  Array.from({ length: count }, (_, i) => entry(from + i, task));
const seqs = (store: ReturnType<typeof createStore>) =>
  [...store.getState().events.wg!.entries.keys()].sort((a, b) => a - b);
const list = (task: string, scope = 'wg') => ({ scope, query: { task } });

describe('event store eviction and list boundaries', () => {
  it('evicts by scope seq, oldest first, whatever the order of writing', () => {
    const store = createStore();
    putEvents(store, 'wg', range(100, MAX_EVENTS));
    putEvents(store, 'wg', [entry(1), entry(2)]);
    const kept = seqs(store);
    expect(kept).toHaveLength(MAX_EVENTS);
    expect(kept.slice(0, 2)).toEqual([100, 101]);
  });

  it('raises the boundary of each list that held an evicted event at or above it', () => {
    const store = createStore();
    setPage(store, 'all', { status: 'ready', before: null, list: list('t') });
    setPage(store, 'deep', { status: 'ready', before: 2, list: list('t') });
    setPage(store, 'high', { status: 'ready', before: 4000, list: list('t') });
    setPage(store, 'other task', { status: 'ready', before: null, list: list('u') });
    setPage(store, 'other scope', { status: 'ready', before: null, list: list('t', 'wg2') });
    setPage(store, 'no list', { status: 'ready', before: null });
    putEvents(store, 'wg', range(1, MAX_EVENTS + 3));
    const pages = store.getState().pages;
    expect(pages.all!.before).toBe(4);
    expect(pages.deep!.before).toBe(4);
    expect(pages.high!.before).toBe(4000);
    expect(pages['other task']!.before).toBeNull();
    expect(pages['other scope']!.before).toBeNull();
    expect(pages['no list']!.before).toBeNull();
    expect(boundaryOf(pages.all)).toBe(4);
    expect(boundaryOf(undefined)).toBe(0);
    // an eviction that no list held changes no paging state
    putEvents(store, 'wg', [entry(0, 'v')]);
    expect(store.getState().pages).toBe(pages);
  });

  it('keeps what a mounted list shows until twice the bound, then evicts oldest first', () => {
    const store = createStore();
    setPage(store, 'back', { status: 'ready', before: 1, list: list('t') });
    setPage(store, 'unmounted', { status: 'ready', before: 1, list: list('u') });
    const release = show(store, { scope: 'wg', task: 't', key: 'back' });
    show(store, { scope: 'wg', task: 'x' });
    putEvents(store, 'wg', range(1, 10, 'u'));
    putEvents(store, 'wg', range(11, MAX_EVENTS));
    // the unmounted list's events go, the shown list's stay
    expect(seqs(store)[0]).toBe(11);
    expect(seqs(store)).toHaveLength(MAX_EVENTS);
    expect(store.getState().pages.unmounted!.before).toBe(11);
    putEvents(store, 'wg', range(MAX_EVENTS + 11, MAX_EVENTS));
    expect(seqs(store)).toHaveLength(2 * MAX_EVENTS);
    expect(store.getState().pages.back!.before).toBe(1);
    putEvents(store, 'wg', range(2 * MAX_EVENTS + 11, 2));
    expect(seqs(store)).toHaveLength(2 * MAX_EVENTS);
    expect(seqs(store)[0]).toBe(13);
    expect(store.getState().pages.back!.before).toBe(13);
    // unmounted: the list's events are evicted like any other
    release();
    putEvents(store, 'wg', range(3 * MAX_EVENTS, 1));
    expect(seqs(store)).toHaveLength(MAX_EVENTS);
  });

  it('a mounted list that holds everything keeps all of it', () => {
    const store = createStore();
    setPage(store, 'all', { status: 'ready', before: null, list: list('t') });
    show(store, { scope: 'wg', task: 't', key: 'all' });
    putEvents(store, 'wg', range(1, MAX_EVENTS + 1));
    expect(seqs(store)).toHaveLength(MAX_EVENTS + 1);
  });

  it('a list no view shows keeps nothing beyond the bound', () => {
    const store = createStore();
    setPage(store, 'newest', { status: 'ready', before: 1, list: list('t') });
    show(store, { scope: 'wg', task: 't', key: 'other key' });
    putEvents(store, 'wg', range(1, MAX_EVENTS + 1));
    expect(seqs(store)[0]).toBe(2);
    expect(store.getState().pages.newest!.before).toBe(2);
  });

  it('a quiet chat opened while other tasks filled the scope keeps its fresh newest page (review KOR-35)', () => {
    const store = createStore();
    // other tasks wrote the bound full of newer bulk events
    putEvents(store, 'wg', range(101, MAX_EVENTS, 'u'));
    // the view opens the quiet chat: its newest page (older events) arrives with its boundary set
    setPage(store, 'chat', { status: 'ready', before: 10, list: list('t') });
    show(store, { scope: 'wg', task: 't', key: 'chat' });
    putEvents(store, 'wg', range(10, 11));
    const kept = seqs(store);
    expect(kept).toHaveLength(MAX_EVENTS);
    expect(kept.slice(0, 11)).toEqual(Array.from({ length: 11 }, (_, i) => 10 + i));
    expect(kept[11]).toBe(112);
    expect(store.getState().pages.chat!.before).toBe(10);
  });

  it('dropping keeps what another mounted list holds at or above its boundary (review KOR-35)', () => {
    const store = createStore();
    // the task chat was read back to 1; the session list of the task re-reads its newest page from 20
    setPage(store, 'task', { status: 'ready', before: 1, list: list('t') });
    setPage(store, 'session', {
      status: 'ready',
      before: 20,
      list: { scope: 'wg', query: { task: 't', session: 's' } },
    });
    show(store, { scope: 'wg', task: 't', key: 'task' });
    const own = (seq: number) => ({
      ...entry(seq),
      event: { header: { kind: 'session.output', task: 't', session: 's' } },
    });
    putEvents(store, 'wg', [own(5), own(6), entry(7, 'u'), own(25)]);
    dropEvents(store, 'wg', (e) => e.scopeSeq < 20);
    expect(seqs(store)).toEqual([5, 6, 25]);
    expect(store.getState().pages.task!.before).toBe(1);
    // once the task chat moved its boundary above them, they go
    const pages = store.getState().pages;
    setPage(store, 'task', { ...pages.task!, before: 30 });
    dropEvents(store, 'wg', (e) => e.scopeSeq < 20);
    expect(seqs(store)).toEqual([25]);
  });

  it('dropping raises the boundaries of the lists that held the dropped events', () => {
    const store = createStore();
    setPage(store, 'session', {
      status: 'ready',
      before: 5,
      list: { scope: 'wg', query: { task: 't', session: 's' } },
    });
    putEvents(store, 'wg', [
      { ...entry(6), event: { header: { kind: 'session.output', task: 't', session: 's' } } },
      entry(7),
    ]);
    dropEvents(store, 'wg', (e) => e.scopeSeq === 7);
    expect(store.getState().pages.session!.before).toBe(5);
    dropEvents(store, 'wg', (e) => e.scopeSeq === 6);
    expect(store.getState().pages.session!.before).toBe(7);
    expect(seqs(store)).toEqual([]);
  });
});
