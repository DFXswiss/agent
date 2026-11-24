// The sliding window of every list of the web client (ADR-063 section 1): which rows of an ordered list are rendered.
// Pure; the React side is `ui/lists/WindowedList.tsx`.
//
// The functions work on the list's keys in display order. `newest` says at which end new items appear: `end` for
// ascending lists (chat, logs), `start` for the newest-first lists (tasks, audit, notifications). Internally every
// index is "oriented": 0 is the oldest item and `n - 1` the newest, whatever the display order.
//
// Two modes:
// - follow: the window ends at the newest item and moves with new items. It keeps its oldest row while it grows (no
//   jump under the reader) until it holds `maxRows` rows; beyond that it drops rows at the oldest end.
// - anchored: the window keeps its oldest row while items arrive; newer items stay hidden behind a count. Any move
//   away from the newest end switches to it.
// The window never holds more than `maxRows` rows: a move past it drops rows at the other end. A missing anchor (the
// row left the list) falls back to the nearest end.

export type WindowMode = 'follow' | 'anchored';
export type NewestAt = 'start' | 'end';

export interface WindowConfig {
  /** Rows shown at first and after "Jump to the newest". */
  readonly size: number;
  /** Rows added by one move ("Show earlier", "Show more"). */
  readonly page: number;
  /** The most rows the window ever holds. */
  readonly maxRows: number;
  /** The display end at which new items appear. */
  readonly newest: NewestAt;
}

export interface WindowState {
  readonly mode: WindowMode;
  /** Key of the oldest shown row; `null` before the first row is known. */
  readonly anchor: string | null;
  /** Oriented index of the anchor when it was last seen: decides the nearest end when it disappears. */
  readonly at: number;
  /** Rows the window holds (between `size` and `maxRows`). */
  readonly size: number;
}

export interface WindowRange {
  /** First shown row in display order (inclusive). */
  readonly from: number;
  /** End of the shown rows in display order (exclusive). */
  readonly to: number;
  /** Hidden rows before `from` and after `to` in display order. */
  readonly before: number;
  readonly after: number;
  /** Hidden rows on the newest and on the oldest side. */
  readonly newer: number;
  readonly older: number;
}

/** A configuration with sane numbers: every value at least 1, `size` and `page` at most `maxRows`. */
export function windowConfig(config: WindowConfig): WindowConfig {
  const maxRows = Math.max(1, Math.floor(config.maxRows));
  const clamp = (value: number) => Math.min(maxRows, Math.max(1, Math.floor(value)));
  return { size: clamp(config.size), page: clamp(config.page), maxRows, newest: config.newest };
}

/** The start state: following the newest items with `size` rows. */
export function initialWindow(config: WindowConfig): WindowState {
  return { mode: 'follow', anchor: null, at: 0, size: config.size };
}

const oriented = (keys: readonly string[], key: string, newest: NewestAt): number => {
  const index = keys.indexOf(key);
  return index < 0 || newest === 'end' ? index : keys.length - 1 - index;
};

const keyAt = (keys: readonly string[], at: number, newest: NewestAt): string =>
  keys[newest === 'end' ? at : keys.length - 1 - at]!;

interface Span {
  readonly mode: WindowMode;
  readonly lo: number;
  readonly hi: number;
  /** Rows the window asks for (an anchored window near the newest end shows fewer). */
  readonly size: number;
}

/** The oriented span `[lo, hi)` the state shows. */
function span(keys: readonly string[], state: WindowState, config: WindowConfig): Span {
  const n = keys.length;
  const size = Math.min(config.maxRows, Math.max(config.size, state.size));
  const at = state.anchor === null ? -1 : oriented(keys, state.anchor, config.newest);
  const tail = Math.max(0, n - size);
  // an empty list leaves no anchor: start again at the newest end
  if (state.mode === 'follow' || state.anchor === null) {
    const lo = at < 0 ? tail : Math.min(Math.max(at, n - config.maxRows), tail);
    return { mode: 'follow', lo, hi: n, size: n - lo };
  }
  if (at >= 0) return { mode: 'anchored', lo: at, hi: Math.min(n, at + size), size };
  // the anchor left the list: the nearest end
  if (state.at <= n - state.at) return { mode: 'anchored', lo: 0, hi: Math.min(n, size), size };
  return { mode: 'follow', lo: tail, hi: n, size: n - tail };
}

function stateOf(keys: readonly string[], { mode, lo, hi, size }: Span, config: WindowConfig): WindowState {
  return {
    mode,
    anchor: lo < hi ? keyAt(keys, lo, config.newest) : null,
    at: lo,
    size: Math.min(config.maxRows, Math.max(config.size, size)),
  };
}

/**
 * The state normalised to the current keys (anchor = oldest shown row). Idempotent; a list renders with the settled
 * state so that the anchor follows the rows that are actually shown.
 */
export function settleWindow(keys: readonly string[], state: WindowState, config: WindowConfig): WindowState {
  return stateOf(keys, span(keys, state, config), config);
}

/** Whether two states are the same (to skip a state update). */
export function sameWindow(a: WindowState, b: WindowState): boolean {
  return a.mode === b.mode && a.anchor === b.anchor && a.at === b.at && a.size === b.size;
}

/** The shown rows in display order and the hidden counts. */
export function windowRange(keys: readonly string[], state: WindowState, config: WindowConfig): WindowRange {
  const n = keys.length;
  const { lo, hi } = span(keys, state, config);
  const from = config.newest === 'end' ? lo : n - hi;
  const to = config.newest === 'end' ? hi : n - lo;
  return { from, to, before: from, after: n - to, newer: n - hi, older: lo };
}

/** One page more on the oldest side; past `maxRows` rows drop at the newest end (the window becomes anchored). */
export function showOlder(keys: readonly string[], state: WindowState, config: WindowConfig): WindowState {
  const n = keys.length;
  const current = span(keys, state, config);
  const lo = Math.max(0, current.lo - config.page);
  const hi = Math.min(current.hi, lo + config.maxRows);
  const mode = hi === n && current.mode === 'follow' ? 'follow' : 'anchored';
  return stateOf(keys, { mode, lo, hi, size: hi - lo }, config);
}

/** One page more on the newest side; past `maxRows` rows drop at the oldest end. Reaching the newest item follows. */
export function showNewer(keys: readonly string[], state: WindowState, config: WindowConfig): WindowState {
  const n = keys.length;
  const current = span(keys, state, config);
  const hi = Math.min(n, current.hi + config.page);
  const lo = Math.max(current.lo, hi - config.maxRows);
  return stateOf(keys, { mode: hi === n ? 'follow' : 'anchored', lo, hi, size: hi - lo }, config);
}

/** "Jump to the newest": follow again with `size` rows. */
export function showNewest(config: WindowConfig): WindowState {
  return initialWindow(config);
}
