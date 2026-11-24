// U-WEB-lists-window (ADR-063 section 1): the sliding window of every list – follow and anchored modes, the `maxRows`
// cap, moves by a page, a missing anchor, both display orders – and the closed set of list ids.
import { describe, expect, it } from 'vitest';
import { isListId, LIST_IDS } from '../../../src/core/lists/ids';
import {
  initialWindow,
  sameWindow,
  settleWindow,
  showNewer,
  showNewest,
  showOlder,
  type WindowConfig,
  windowConfig,
  type WindowState,
  windowRange,
} from '../../../src/core/lists/window';

const keys = (from: number, count: number) => Array.from({ length: count }, (_, index) => `k${from + index}`);
const ASC: WindowConfig = { size: 4, page: 2, maxRows: 6, newest: 'end' };
const DESC: WindowConfig = { ...ASC, newest: 'start' };

/** The shown keys of `state` over `list`. */
function shown(list: readonly string[], state: WindowState, config: WindowConfig): string[] {
  const { from, to } = windowRange(list, state, config);
  return list.slice(from, to);
}

describe('windowConfig', () => {
  it('keeps every number between 1 and maxRows', () => {
    expect(windowConfig({ size: 0, page: 500, maxRows: 0.5, newest: 'end' })).toEqual({
      size: 1,
      page: 1,
      maxRows: 1,
      newest: 'end',
    });
    expect(windowConfig({ size: 100.7, page: 50, maxRows: 150, newest: 'start' })).toEqual({
      size: 100,
      page: 50,
      maxRows: 150,
      newest: 'start',
    });
  });
});

describe('follow mode (newest at the end)', () => {
  it('shows the newest rows, keeps its oldest row while it grows and drops beyond maxRows', () => {
    let state = initialWindow(ASC);
    expect(state).toEqual({ mode: 'follow', anchor: null, at: 0, size: 4 });
    // an empty list
    expect(windowRange([], state, ASC)).toEqual({ from: 0, to: 0, before: 0, after: 0, newer: 0, older: 0 });
    expect(settleWindow([], state, ASC)).toEqual({ mode: 'follow', anchor: null, at: 0, size: 4 });
    let list = keys(0, 10);
    expect(shown(list, state, ASC)).toEqual(['k6', 'k7', 'k8', 'k9']);
    expect(windowRange(list, state, ASC)).toEqual({ from: 6, to: 10, before: 6, after: 0, newer: 0, older: 6 });
    state = settleWindow(list, state, ASC);
    expect(state).toEqual({ mode: 'follow', anchor: 'k6', at: 6, size: 4 });
    // two new items: the window grows, its first row stays
    list = keys(0, 12);
    expect(shown(list, state, ASC)).toEqual(['k6', 'k7', 'k8', 'k9', 'k10', 'k11']);
    state = settleWindow(list, state, ASC);
    expect(state).toEqual({ mode: 'follow', anchor: 'k6', at: 6, size: 6 });
    // past maxRows rows drop at the oldest end
    list = keys(0, 13);
    expect(shown(list, state, ASC)).toEqual(['k7', 'k8', 'k9', 'k10', 'k11', 'k12']);
    // idempotent
    const settled = settleWindow(list, state, ASC);
    expect(settleWindow(list, settled, ASC)).toEqual(settled);
  });

  it('starts at the newest rows again when its anchor disappears', () => {
    const state: WindowState = { mode: 'follow', anchor: 'gone', at: 3, size: 4 };
    expect(shown(keys(0, 10), state, ASC)).toEqual(['k6', 'k7', 'k8', 'k9']);
  });

  it('shows a short list completely', () => {
    expect(shown(keys(0, 2), initialWindow(ASC), ASC)).toEqual(['k0', 'k1']);
    expect(settleWindow(keys(0, 2), initialWindow(ASC), ASC)).toEqual({
      mode: 'follow',
      anchor: 'k0',
      at: 0,
      size: 4,
    });
  });
});

describe('moves', () => {
  it('shows older rows by a page, keeps following while the newest row stays and drops past maxRows', () => {
    const list = keys(0, 20);
    let state = settleWindow(list, initialWindow(ASC), ASC);
    state = showOlder(list, state, ASC);
    expect(state).toEqual({ mode: 'follow', anchor: 'k14', at: 14, size: 6 });
    expect(shown(list, state, ASC)).toEqual(['k14', 'k15', 'k16', 'k17', 'k18', 'k19']);
    // the next page would exceed maxRows: the newest rows drop, the window is anchored
    state = showOlder(list, state, ASC);
    expect(state).toEqual({ mode: 'anchored', anchor: 'k12', at: 12, size: 6 });
    expect(windowRange(list, state, ASC)).toEqual({ from: 12, to: 18, before: 12, after: 2, newer: 2, older: 12 });
    // an anchored window keeps its rows while new items arrive
    const grown = keys(0, 25);
    expect(shown(grown, state, ASC)).toEqual(['k12', 'k13', 'k14', 'k15', 'k16', 'k17']);
    // newer rows by a page; the oldest drop
    state = showNewer(grown, state, ASC);
    expect(state).toEqual({ mode: 'anchored', anchor: 'k14', at: 14, size: 6 });
    // reaching the newest item follows again
    state = showNewer(grown, showNewer(grown, showNewer(grown, state, ASC), ASC), ASC);
    expect(state.mode).toBe('follow');
    expect(shown(grown, state, ASC)).toEqual(['k19', 'k20', 'k21', 'k22', 'k23', 'k24']);
    // older stops at the first row
    let top: WindowState = { mode: 'anchored', anchor: 'k1', at: 1, size: 4 };
    top = showOlder(grown, top, ASC);
    expect(shown(grown, top, ASC)).toEqual(['k0', 'k1', 'k2', 'k3', 'k4']);
    expect(showNewest(ASC)).toEqual(initialWindow(ASC));
  });

  it('keeps an anchored window anchored when an older page still reaches the newest row', () => {
    const list = keys(0, 6);
    const state: WindowState = { mode: 'anchored', anchor: 'k2', at: 2, size: 4 };
    expect(showOlder(list, state, ASC)).toEqual({ mode: 'anchored', anchor: 'k0', at: 0, size: 6 });
  });
});

describe('anchored mode', () => {
  it('falls back to the nearest end when its anchor disappears', () => {
    const list = keys(0, 10);
    const near: WindowState = { mode: 'anchored', anchor: 'gone', at: 2, size: 4 };
    expect(shown(list, near, ASC)).toEqual(['k0', 'k1', 'k2', 'k3']);
    expect(settleWindow(list, near, ASC)).toEqual({ mode: 'anchored', anchor: 'k0', at: 0, size: 4 });
    const far: WindowState = { mode: 'anchored', anchor: 'gone', at: 8, size: 4 };
    expect(shown(list, far, ASC)).toEqual(['k6', 'k7', 'k8', 'k9']);
    expect(settleWindow(list, far, ASC).mode).toBe('follow');
  });

  it('starts at the newest end after the list was empty', () => {
    const empty = settleWindow([], { mode: 'anchored', anchor: 'k3', at: 3, size: 4 }, ASC);
    expect(empty).toEqual({ mode: 'follow', anchor: null, at: 0, size: 4 });
    // an anchored state without an anchor (nothing shown yet) starts at the newest end as well
    const unset: WindowState = { mode: 'anchored', anchor: null, at: 3, size: 4 };
    expect(shown(keys(0, 10), unset, ASC)).toEqual(['k6', 'k7', 'k8', 'k9']);
  });

  it('keeps the asked size near the newest end', () => {
    const list = keys(0, 10);
    const state: WindowState = { mode: 'anchored', anchor: 'k8', at: 8, size: 6 };
    expect(shown(list, state, ASC)).toEqual(['k8', 'k9']);
    expect(settleWindow(list, state, ASC)).toEqual(state);
  });
});

describe('newest first', () => {
  it('mirrors every move: the newest rows at the start, older rows at the end', () => {
    const list = keys(0, 10).reverse(); // k9 … k0
    let state = settleWindow(list, initialWindow(DESC), DESC);
    expect(state).toEqual({ mode: 'follow', anchor: 'k6', at: 6, size: 4 });
    expect(windowRange(list, state, DESC)).toEqual({ from: 0, to: 4, before: 0, after: 6, newer: 0, older: 6 });
    // new items at the start: the window grows there
    const grown = ['k11', 'k10', ...list];
    expect(shown(grown, state, DESC)).toEqual(['k11', 'k10', 'k9', 'k8', 'k7', 'k6']);
    state = showOlder(grown, settleWindow(grown, state, DESC), DESC);
    expect(state.mode).toBe('anchored');
    expect(shown(grown, state, DESC)).toEqual(['k9', 'k8', 'k7', 'k6', 'k5', 'k4']);
    expect(windowRange(grown, state, DESC)).toEqual({ from: 2, to: 8, before: 2, after: 4, newer: 2, older: 4 });
    // loaded older items at the end do not move the window
    const loaded = [...grown, 'm1', 'm0'];
    expect(shown(loaded, state, DESC)).toEqual(['k9', 'k8', 'k7', 'k6', 'k5', 'k4']);
    expect(shown(loaded, showNewer(loaded, state, DESC), DESC)).toEqual(['k11', 'k10', 'k9', 'k8', 'k7', 'k6']);
  });
});

describe('sameWindow', () => {
  it('compares every field', () => {
    const base: WindowState = { mode: 'follow', anchor: 'a', at: 1, size: 4 };
    expect(sameWindow(base, { ...base })).toBe(true);
    expect(sameWindow(base, { ...base, mode: 'anchored' })).toBe(false);
    expect(sameWindow(base, { ...base, anchor: 'b' })).toBe(false);
    expect(sameWindow(base, { ...base, at: 2 })).toBe(false);
    expect(sameWindow(base, { ...base, size: 5 })).toBe(false);
  });
});

describe('list ids', () => {
  it('is a closed set of unique dotted names', () => {
    expect(new Set(LIST_IDS).size).toBe(LIST_IDS.length);
    for (const id of LIST_IDS) expect(id).toMatch(/^[a-z][a-z-]*(?:\.[a-z][a-z-]*)*$/);
    expect(isListId('task.chat')).toBe(true);
    expect(isListId('task.unknown')).toBe(false);
    expect(isListId(7)).toBe(false);
  });
});
