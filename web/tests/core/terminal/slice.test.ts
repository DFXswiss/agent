// The `terminal` slice: strict `scope_seq` order, held chunks behind possible gaps, pulled pages with unverifiable and
// not yet readable chunks, event-ID positions, the bounded tail (newest page, trim, epoch), the history window (copy,
// live growth, earlier pages, its bound), open/close and the `terminals_refused` frame.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import { putEvents } from '../../../src/core/store/slices/events';
import {
  frames,
  HISTORY_CHARS,
  KEEP_CHARS,
  terminalFailed,
  type TerminalEntry,
} from '../../../src/core/store/slices/terminal';
import {
  accountedIn,
  applyEarlier,
  applyNewest,
  applyPage,
  type ChunkVerdict,
  closeHistory,
  closeTerminal,
  earlierFailed,
  earlierLoading,
  earlierPage,
  earlierPageFailed,
  loadingEarlier,
  newestPage,
  openHistory,
  openTerminal,
  pulledPage,
  pulledVerdict,
  receive,
  receiveChunks,
  release,
  releaseHeld,
  setHistory,
  trimTail,
  verdictOf,
} from '../../../src/core/terminal/entries';
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

/** A verified chunk with `size` characters of output. */
const sized = (seq: number, size: number) => ok(seq, `id-${seq}`, body(seq * 100, [[seq * 100, 'x'.repeat(size)]]));

describe('the bounded tail', () => {
  it('grows by push without copying the chunk list', () => {
    let item = applyPage(opened(), [ok(1)], 1);
    const list = item.chunks;
    item = applyPage(item, [ok(2)], 2);
    item = release(receive(item, ok(3)), all);
    expect(item.chunks).toBe(list);
    expect(seqs(item)).toEqual([1, 2, 3]);
    expect(item.chars).toBe(6);
  });

  it('keeps the newest characters, drops the oldest by an eighth and remembers the earlier output', () => {
    expect(KEEP_CHARS).toBe(8 * 1024 * 1024);
    let item = applyPage(opened(), [sized(1, 40), sized(2, 40), bad(3), sized(4, 40), sized(5, 40)], 5);
    expect(trimTail(item, 160)).toBe(item);
    item = trimTail(item, 100);
    // 160 characters, goal 87.5: two chunks leave, the bad chunk before the first kept one as well
    expect(seqs(item)).toEqual([4, 5]);
    expect(item.chars).toBe(80);
    expect(item.earlier).toBe(4);
    expect(item.bad).toEqual([]);
    expect(item.ids.has('id-1')).toBe(false);
    expect(item.ids.get('id-4')).toBe(4);
    // a dropped ID may come back at another position (it is no longer in memory)
    expect(seqs(applyPage(item, [ok(6, 'id-1')], 6))).toEqual([4, 5, 6]);
    // the newest chunk always stays
    const single = trimTail(applyPage(opened(), [sized(1, 50), sized(2, 500)], 2), 100);
    expect(seqs(single)).toEqual([2]);
    expect(single.earlier).toBe(2);
  });

  it('replaces the tail with the newest page and counts an epoch when it had content', () => {
    let item = applyNewest(opened(), [ok(8), ok(9)], 12, 8);
    expect(seqs(item)).toEqual([8, 9]);
    expect(item).toMatchObject({ through: 12, earlier: 8, epoch: 0, status: 'ready' });
    const empty = applyNewest(opened(), [], 0, null);
    expect(empty).toMatchObject({ through: 0, earlier: null, epoch: 0 });
    // held chunks beyond the page stay, with their IDs
    item = receive(item, ok(20));
    item = openHistory(item);
    item = applyNewest(item, [bad(15)], 16, 15);
    expect(seqs(item)).toEqual([]);
    expect(item).toMatchObject({ through: 16, earlier: 15, epoch: 1, bad: [{ seq: 15, code: 'signature_invalid' }] });
    expect(item.history!.cut).toBe(true);
    expect(item.held.map((chunk) => chunk.seq)).toEqual([20]);
    expect(applyPage(item, [ok(19, 'id-20')], 19).bad.at(-1)).toEqual({ seq: 19, code: 'event_conflict' });
    // only bad chunks were in it: still an epoch; an open window without content is kept as it is
    const again = applyNewest(item, [], 30, null);
    expect(again.epoch).toBe(2);
    const plain = openHistory(empty);
    expect(applyNewest(plain, [], 1, null).history).toBe(plain.history);
  });
});

describe('the history window', () => {
  const windowOf = (item: TerminalEntry) => item.history!;
  const windowSeqs = (item: TerminalEntry) => windowOf(item).chunks.map((chunk) => chunk.seq);

  it('starts as a copy of the tail and grows with live chunks until its bound', () => {
    let item = applyNewest(opened(), [ok(5), bad(6), ok(7)], 7, 5);
    expect(closeHistory(item)).toBe(item);
    item = openHistory(item);
    expect(openHistory(item)).toBe(item);
    expect(windowOf(item)).toMatchObject({ before: 5, cut: false, status: 'ready', code: null, chars: 4 });
    expect(windowOf(item).chunks).not.toBe(item.chunks);
    expect(windowOf(item).bad).toEqual([{ seq: 6, code: 'signature_invalid' }]);
    item = release(receive(item, ok(8)), all);
    expect(windowSeqs(item)).toEqual([5, 7, 8]);
    // bad chunks of a later pull are recorded in the window as well
    item = applyPage(item, [bad(9, 'header_invalid')], 9);
    expect(windowOf(item).bad.at(-1)).toEqual({ seq: 9, code: 'header_invalid' });
    // at its bound it stops taking live chunks and says so
    item = { ...item, history: { ...windowOf(item), chars: HISTORY_CHARS } };
    item = release(receive(item, ok(10)), all);
    expect(windowSeqs(item)).toEqual([5, 7, 8]);
    expect(windowOf(item).cut).toBe(true);
    item = release(receive(item, ok(11)), all);
    expect(windowSeqs(item)).toEqual([5, 7, 8]);
    // a cut window takes no bad chunks either
    item = applyPage(item, [bad(12)], 12);
    expect(windowOf(item).bad).toHaveLength(2);
    expect(seqs(item)).toEqual([5, 7, 8, 10, 11]);
    expect(closeHistory(item).history).toBeNull();
  });

  it('loads earlier pages in front, newest first from the hub, and records bad ones', () => {
    let item = openHistory(applyNewest(opened(), [ok(5)], 5, 5));
    expect(earlierLoading(item, 4)).toBe(item);
    item = earlierLoading(item, 5);
    expect(windowOf(item).status).toBe('loading');
    expect(earlierLoading(item, 5)).toBe(item);
    // a page for another cursor is ignored
    expect(applyEarlier(item, 4, [ok(1)], null)).toBe(item);
    item = applyEarlier(item, 5, [ok(3), bad(2), ok(5), ok(4, 'id-5'), ok(1)], 1);
    expect(windowSeqs(item)).toEqual([1, 3, 5]);
    expect(windowOf(item)).toMatchObject({ before: 1, status: 'ready', cut: false });
    expect(windowOf(item).bad).toEqual([
      { seq: 2, code: 'signature_invalid' },
      { seq: 4, code: 'event_conflict' },
    ]);
    expect(seqs(item)).toEqual([5]);
    // a chunk that cannot be read yet fails the load; nothing is applied
    const failed = applyEarlier(item, 1, [ok(0), waiting(0)], null);
    expect(windowOf(failed)).toMatchObject({ status: 'error', code: 'unavailable', before: 1 });
    expect(windowSeqs(failed)).toEqual([1, 3, 5]);
    expect(earlierFailed(item, 7, 'x')).toBe(item);
    // at the start of the recording
    item = applyEarlier(item, 1, [ok(0)], null);
    expect(windowOf(item).before).toBeNull();
    expect(earlierLoading(item, 1)).toBe(item);
  });

  it('drops the newest chunks of a window beyond its bound and keeps the page', () => {
    let item = openHistory(applyNewest(opened(), [sized(8, 30), sized(9, 30)], 9, 8));
    item = applyEarlier(item, 8, [sized(6, 30), sized(7, 30)], 6, 70);
    expect(windowSeqs(item)).toEqual([6, 7]);
    expect(windowOf(item)).toMatchObject({ chars: 60, cut: true, before: 6 });
    expect(windowOf(item).ids.has('id-8')).toBe(false);
    item = applyEarlier(item, 6, [sized(5, 100)], null, 70);
    expect(windowSeqs(item)).toEqual([5]);
  });

  it('works on entries without a window', () => {
    const item = opened();
    expect(earlierLoading(item, 1)).toBe(item);
    expect(earlierFailed(item, 1, 'x')).toBe(item);
    expect(applyEarlier(item, 1, [], null)).toBe(item);
  });

  it('has store actions for the newest page, the mode and earlier pages', () => {
    const store = createStore();
    openTerminal(store, TARGET);
    const state = () => store.getState().terminal.sessions[SESSION]!;
    newestPage(store, SESSION, [ok(4)], 4, 4, none);
    expect(state()).toMatchObject({ earlier: 4, through: 4 });
    setHistory(store, SESSION, true);
    loadingEarlier(store, SESSION, 4);
    expect(state().history!.status).toBe('loading');
    earlierPageFailed(store, SESSION, 4, 'rate_limited');
    expect(state().history).toMatchObject({ status: 'error', code: 'rate_limited' });
    earlierPage(store, SESSION, 4, [ok(2)], null);
    expect(state().history!.chunks.map((chunk) => chunk.seq)).toEqual([2, 4]);
    setHistory(store, SESSION, false);
    expect(state().history).toBeNull();
  });
});
