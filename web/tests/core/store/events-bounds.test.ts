// The bounds of the event store (ADR-063 section 1): count and bytes per scope, in two classes, so that bulk session
// events never evict titles or whiteboards; the class and size of an entry; dropping entries before a hole.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import {
  classOf,
  dropEvents,
  type EventEntry,
  kindOf,
  MAX_BYTES,
  MAX_EVENTS,
  putEvents,
  sizeOf,
} from '../../../src/core/store/slices/events';

function entry(seq: number, kind: string | null, ct = 'ab', bodyBytes?: number, checked = false): EventEntry {
  return {
    org: 'o',
    scopeId: 'wg',
    scopeSeq: seq,
    receivedAt: null,
    event: { header: kind === null ? {} : { kind }, ct },
    status: checked ? 'verified' : 'pending',
    code: null,
    header: checked ? { kind } : null,
    body: null,
    ...(bodyBytes !== undefined && { bodyBytes }),
  };
}

const seqs = (store: ReturnType<typeof createStore>) => [...store.getState().events.wg!.entries.keys()];

describe('event store bounds', () => {
  it('classifies by the checked or the raw kind and sizes ciphertext plus body', () => {
    expect(kindOf(entry(1, 'session.output', 'x', 0, true))).toBe('session.output');
    expect(kindOf(entry(1, 'task.meta'))).toBe('task.meta');
    expect(kindOf({ ...entry(1, null), event: 'raw' })).toBeNull();
    expect(classOf(entry(1, 'session.tool'))).toBe('bulk');
    expect(classOf(entry(1, 'whiteboard.entry'))).toBe('other');
    expect(classOf(entry(1, null))).toBe('other');
    expect(sizeOf(entry(1, null, 'abcd', 10))).toBe(14);
    expect(sizeOf({ ...entry(1, null), event: null })).toBe(0);
  });

  it('evicts the least recently used bulk entries by count without touching the other class', () => {
    const store = createStore();
    putEvents(store, 'wg', [entry(1, 'task.meta'), entry(2, 'whiteboard.entry')]);
    putEvents(
      store,
      'wg',
      Array.from({ length: MAX_EVENTS + 3 }, (_, i) => entry(i + 10, 'session.output')),
    );
    const kept = seqs(store);
    expect(kept).toHaveLength(MAX_EVENTS + 2);
    expect(kept.slice(0, 3)).toEqual([1, 2, 13]);
  });

  it('evicts by bytes as well', () => {
    const store = createStore();
    const half = MAX_BYTES / 2 - 1;
    putEvents(store, 'wg', [entry(1, 'session.file', 'x', half), entry(2, 'task.meta', 'x', half)]);
    putEvents(store, 'wg', [entry(3, 'session.file', 'x', half)]);
    expect(seqs(store)).toEqual([1, 2, 3]);
    putEvents(store, 'wg', [entry(4, 'session.file', 'x', 10)]);
    expect(seqs(store)).toEqual([2, 3, 4]);
  });

  it('drops the entries a predicate names (no update when none matches)', () => {
    const store = createStore();
    dropEvents(store, 'wg', () => true);
    expect(store.getState().events).toEqual({});
    putEvents(store, 'wg', [entry(1, 'session.output'), entry(2, 'prompt')]);
    const before = store.getState().events;
    dropEvents(store, 'wg', () => false);
    expect(store.getState().events).toBe(before);
    dropEvents(store, 'wg', (e) => classOf(e) === 'bulk');
    expect(seqs(store)).toEqual([2]);
  });
});
