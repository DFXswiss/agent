// The `terminal` slice: strict `scope_seq` order, held chunks behind possible gaps, pulled pages with unverifiable and
// not yet readable chunks, event-ID positions, open/close and the `terminals_refused` frame.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import { putEvents } from '../../../src/core/store/slices/events';
import {
  accountedIn,
  applyPage,
  type ChunkVerdict,
  closeTerminal,
  frames,
  openTerminal,
  pulledPage,
  pulledVerdict,
  receive,
  receiveChunks,
  release,
  releaseHeld,
  terminalFailed,
  type TerminalEntry,
  verdictOf,
} from '../../../src/core/store/slices/terminal';
import { body, entry, other, OTHER, SESSION, TARGET, WG } from './helpers';

const ok = (seq: number, id = `id-${seq}`, payload: unknown = body(seq * 100, [[seq * 100, `c${seq}`]])) =>
  ({ seq, status: 'verified', id, body: payload }) as ChunkVerdict;
const bad = (seq: number, code = 'signature_invalid') => ({ seq, status: 'unverifiable', code }) as ChunkVerdict;
const waiting = (seq: number) => ({ seq, status: 'waiting' }) as ChunkVerdict;

function opened(): TerminalEntry {
  const store = createStore();
  openTerminal(store, TARGET);
  return store.getState().terminal.sessions[SESSION]!;
}

const seqs = (item: TerminalEntry) => item.chunks.map((chunk) => chunk.seq);
const none = () => false;
const all = () => true;

describe('verdicts and accounting', () => {
  it('turns settled entries into verdicts', () => {
    expect(verdictOf(entry(4))).toMatchObject({ seq: 4, status: 'verified', id: 'id-4' });
    expect(verdictOf(entry(5, { status: 'unverifiable', code: 'author_not_writer' }))).toEqual({
      seq: 5,
      status: 'unverifiable',
      code: 'author_not_writer',
    });
    expect(verdictOf(entry(6, { status: 'locked' }))).toEqual({ seq: 6, status: 'waiting' });
    expect(verdictOf(entry(7, { status: 'pending' }))).toEqual({ seq: 7, status: 'waiting' });
  });

  it('refuses pulled events that are not chunks of the session', () => {
    expect(pulledVerdict(entry(1), SESSION)).toMatchObject({ status: 'verified' });
    expect(pulledVerdict(entry(2, { session: OTHER }), SESSION)).toEqual({
      seq: 2,
      status: 'unverifiable',
      code: 'header_invalid',
    });
    expect(pulledVerdict(other(3), SESSION)).toMatchObject({ status: 'unverifiable', code: 'header_invalid' });
    expect(pulledVerdict(entry(4, { status: 'locked' }), SESSION)).toEqual({ seq: 4, status: 'waiting' });
  });

  it('accounts only for verified events that are not chunks of the session', () => {
    const store = createStore();
    putEvents(store, WG, [
      other(1),
      entry(2),
      entry(3, { session: OTHER }),
      entry(4, { status: 'pending' }),
      entry(5, { status: 'unverifiable' }),
    ]);
    const accounted = accountedIn(store.getState().events, TARGET);
    expect([1, 2, 3, 4, 5, 6].map(accounted)).toEqual([true, false, true, false, false, false]);
    expect(accountedIn({}, TARGET)(1)).toBe(false);
  });
});

describe('live chunks', () => {
  it('applies a chunk at once when every seq before it is accounted for, else holds it', () => {
    let item = applyPage(opened(), [], 10);
    item = release(receive(item, ok(12)), (seq) => seq === 11);
    expect(seqs(item)).toEqual([12]);
    expect(item.through).toBe(12);
    item = release(receive(item, ok(15)), (seq) => seq === 13);
    expect(seqs(item)).toEqual([12]);
    expect(item.held.map((chunk) => chunk.seq)).toEqual([15]);
    // the same chunk again, and a chunk already applied, change nothing
    expect(receive(item, ok(15))).toBe(item);
    expect(receive(item, ok(12))).toBe(item);
    expect(release(item, (seq) => seq === 13)).toBe(item);
    item = release(item, all);
    expect(seqs(item)).toEqual([12, 15]);
    expect(item.held).toEqual([]);
  });

  it('applies held chunks in seq order, even when they arrived out of order', () => {
    let item = applyPage(opened(), [], 0);
    item = receive(item, ok(3));
    item = receive(item, ok(1));
    item = receive(item, ok(2));
    expect(seqs(release(item, none))).toEqual([1, 2, 3]);
  });

  it('ignores unread or broken live chunks (the pull records them)', () => {
    const item = applyPage(opened(), [], 0);
    expect(receive(item, bad(1))).toBe(item);
    expect(receive(item, waiting(1))).toBe(item);
    expect(receive(item, ok(1, 'id-1', { nonsense: true }))).toBe(item);
    const placed = applyPage(item, [ok(1, 'x')], 1);
    expect(receive(placed, ok(2, 'x'))).toBe(placed);
  });

  it('drops held chunks that a pull already covered', () => {
    let item = applyPage(opened(), [], 0);
    item = receive(item, ok(5));
    item = { ...item, through: 6 };
    expect(release(item, none).held).toEqual([]);
  });
});

describe('pulled pages', () => {
  it('applies a page in order up to its end and records refused chunks', () => {
    let item = opened();
    expect(item.status).toBe('loading');
    item = applyPage(item, [ok(4), bad(2), ok(1), ok(6, 'id-6', { nope: 1 })], 8);
    expect(seqs(item)).toEqual([1, 4]);
    expect(item.bad).toEqual([
      { seq: 2, code: 'signature_invalid' },
      { seq: 6, code: 'header_invalid' },
    ]);
    expect(item.through).toBe(8);
    expect(item.status).toBe('ready');
    // an overlapping page skips what is known
    item = applyPage(item, [ok(4), ok(9)], 9);
    expect(seqs(item)).toEqual([1, 4, 9]);
  });

  it('refuses a second position of one event ID', () => {
    const item = applyPage(opened(), [ok(1, 'same'), ok(2, 'same')], 2);
    expect(seqs(item)).toEqual([1]);
    expect(item.bad).toEqual([{ seq: 2, code: 'event_conflict' }]);
  });

  it('blocks at a chunk that cannot be read yet and keeps the later ones held', () => {
    let item = applyPage(opened(), [ok(1), waiting(3), ok(5), bad(6), ok(7), ok(8, 'id-1')], 9);
    expect(seqs(item)).toEqual([1]);
    expect(item.through).toBe(2);
    expect(item.blockedAt).toBe(3);
    expect(item.held.map((chunk) => chunk.seq)).toEqual([5, 7]);
    // nothing passes the block, even if accounted for
    expect(release(item, all)).toBe(item);
    // a live chunk beyond the block waits as well
    item = release(receive(item, ok(10)), all);
    expect(item.held.map((chunk) => chunk.seq)).toEqual([5, 7, 10]);
    // a live copy of a held chunk is not held twice
    expect(applyPage({ ...item, through: 2, blockedAt: null }, [waiting(3), ok(5)], 9).held).toHaveLength(3);
    // the next pull reads it
    item = applyPage(item, [ok(3), ok(5), bad(6), ok(7)], 9);
    expect(item.blockedAt).toBeNull();
    expect(seqs(item)).toEqual([1, 3, 5, 7]);
    expect(item.through).toBe(9);
    item = release(item, all);
    expect(seqs(item)).toEqual([1, 3, 5, 7, 10]);
  });
});

describe('store actions', () => {
  it('opens once, receives, pulls, releases, fails and closes', () => {
    const store = createStore();
    openTerminal(store, TARGET);
    const first = store.getState().terminal;
    openTerminal(store, TARGET);
    expect(store.getState().terminal).toBe(first);
    pulledPage(store, SESSION, [ok(1)], 1, none);
    receiveChunks(store, SESSION, [ok(3)], none);
    const held = store.getState().terminal;
    expect(held.sessions[SESSION]!.held).toHaveLength(1);
    releaseHeld(store, SESSION, none);
    expect(store.getState().terminal).toBe(held);
    releaseHeld(store, SESSION, all);
    expect(seqs(store.getState().terminal.sessions[SESSION]!)).toEqual([1, 3]);
    terminalFailed(store, SESSION, 'unavailable');
    const failed = store.getState().terminal;
    expect(failed.sessions[SESSION]).toMatchObject({ status: 'error', code: 'unavailable' });
    terminalFailed(store, SESSION, 'unavailable');
    expect(store.getState().terminal).toBe(failed);
    // actions on a session that is not watched change nothing
    receiveChunks(store, OTHER, [ok(1)], all);
    closeTerminal(store, OTHER);
    expect(store.getState().terminal).toBe(failed);
    closeTerminal(store, SESSION);
    expect(store.getState().terminal.sessions).toEqual({});
  });

  it('marks refused sessions from the live-channel frame', () => {
    const store = createStore();
    openTerminal(store, TARGET);
    frames.terminals_refused!(store, { sessions: [SESSION, 3, OTHER] });
    frames.terminals_refused!(store, { sessions: 'x' });
    frames.terminals_refused!(store, null);
    expect(store.getState().terminal.sessions[SESSION]).toMatchObject({ status: 'error', code: 'not_found' });
    expect(store.getState().terminal.sessions[OTHER]).toBeUndefined();
  });
});
