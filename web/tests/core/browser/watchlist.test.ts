// The watched browsers of the signed-in browser: sessions per org (newest three), the largest bandwidth wish, release
// and the hand-over of the channel's frames to the views.
import { describe, expect, it, vi } from 'vitest';
import { type BrowserListener, BrowserWatchList } from '../../../src/core/browser/watchlist';
import { ORG } from './world';

const listener = (): BrowserListener & Record<string, ReturnType<typeof vi.fn>> => ({
  action: vi.fn(),
  live: vi.fn(),
  resumed: vi.fn(),
  refused: vi.fn(),
});

describe('BrowserWatchList', () => {
  it('lists the newest three sessions of an org and the largest wish of their views', () => {
    const changed = vi.fn();
    const list = new BrowserWatchList(changed);
    expect(list.sessions(ORG)).toEqual([]);
    expect(list.kbps(ORG)).toBe(1000);
    const a = list.watch(ORG, 'a', listener(), 250);
    list.watch('other-org', 'x', listener(), 3000);
    list.watch(ORG, 'b', listener(), 250);
    list.watch(ORG, 'c', listener(), 250);
    const d = list.watch(ORG, 'd', listener(), 250);
    expect(list.sessions(ORG)).toEqual(['b', 'c', 'd']);
    expect(list.kbps(ORG)).toBe(250);
    // a second view of `a` makes it the newest again; the wish of a session out of the list does not count
    const a2 = list.watch(ORG, 'a', listener(), 1000);
    expect(list.sessions(ORG)).toEqual(['c', 'd', 'a']);
    expect(list.kbps(ORG)).toBe(1000);
    a.setKbps(3000);
    expect(list.kbps(ORG)).toBe(3000);
    const calls = changed.mock.calls.length;
    a.setKbps(3000);
    expect(changed).toHaveBeenCalledTimes(calls);
    a.release();
    a.release();
    a.setKbps(250);
    expect(list.kbps(ORG)).toBe(1000);
    expect(list.sessions(ORG)).toEqual(['c', 'd', 'a']);
    a2.release();
    expect(list.sessions(ORG)).toEqual(['b', 'c', 'd']);
    d.release();
    expect(list.sessions(ORG)).toEqual(['b', 'c']);
  });

  it('hands frames to the views of their session and reports the types it handled', () => {
    const list = new BrowserWatchList(() => undefined);
    const a = listener();
    const b = listener();
    list.watch(ORG, 'a', a, 1000);
    list.watch(ORG, 'b', b, 1000);
    const action = { scope_id: 'wg', scope_seq: 4, event: { header: { session: 'a' } } };
    expect(list.dispatch('browser_action', action)).toBe(true);
    expect(list.dispatch('browser_action', { event: null })).toBe(true);
    expect(list.dispatch('browser_action', { event: { header: null } })).toBe(true);
    expect(a.action).toHaveBeenCalledWith(action);
    expect(b.action).not.toHaveBeenCalled();
    const frame = { session_id: 'b', frame: {} };
    expect(list.dispatch('browser_live', frame)).toBe(true);
    expect(list.dispatch('browser_live', null)).toBe(true);
    expect(b.live).toHaveBeenCalledWith(frame);
    expect(a.live).not.toHaveBeenCalled();
    expect(list.dispatch('hello', {})).toBe(false);
    expect(a.resumed).toHaveBeenCalledTimes(1);
    expect(b.resumed).toHaveBeenCalledTimes(1);
    expect(list.dispatch('task', {})).toBe(false);
    expect(list.dispatch('browsers_refused', { sessions: ['a', 7, 'unknown'] })).toBe(true);
    expect(list.dispatch('browsers_refused', null)).toBe(true);
    expect(a.refused).toHaveBeenCalledTimes(1);
    expect(b.refused).not.toHaveBeenCalled();
    expect(list.sessions(ORG)).toEqual(['b']);
  });
});
