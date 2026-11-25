// The browser sessions of a session from its `browser.state` events (06-web.md section 5.4b, ADR-085 section 7):
//
// - `selectBrowserStates`: the valid `browser.state` bodies of one session of a task in scope order, memoized on the
//   scope's events;
// - `currentStates`: per `browser_id` its current state – `closed` stays (nothing follows a close, also when an
//   earlier event arrives late), else the last event;
// - `newestOpen`: the `browser_id` of the session's newest browser session that is not closed (`starting`, `open` or
//   `restarted`; one per session at a time, ADR-085 section 1) – the only one whose live frames are shown;
// - `browserStart`: the countdown of a `starting` browser session to the 10 s target (KONZEPT §3) from `opened_at`
//   (else the event time), "delayed" after it.
import { parseTime } from '../encoding/time';
import { sortedEntries } from '../chat/shape';
import { START_TARGET_MS } from '../chat/workvm';
import type { ScopeEvents } from '../store/slices/events';
import type { StoreState } from '../store/store';
import { type BrowserState, parseBrowserState, STATE_KIND } from './shape';

function collect(scope: ScopeEvents | undefined, task: string, session: string): readonly BrowserState[] {
  const states: BrowserState[] = [];
  for (const entry of sortedEntries(scope)) {
    if (entry.status !== 'verified' || entry.header!.kind !== STATE_KIND) continue;
    if (entry.header!.task !== task || entry.header!.session !== session) continue;
    const body = parseBrowserState(entry.body);
    if (body !== null) states.push(body);
  }
  return states;
}

/** A selector of the valid `browser.state` bodies of `session` in scope order (memoized on the scope's events). */
export function selectBrowserStates(
  wg: string,
  task: string,
  session: string,
): (state: StoreState) => readonly BrowserState[] {
  let last: { scope: unknown; states: readonly BrowserState[] } | null = null;
  return (state) => {
    const scope = state.events[wg];
    if (last !== null && last.scope === scope) return last.states;
    last = { scope, states: collect(scope, task, session) };
    return last.states;
  };
}

/** Per `browser_id` the current state (module comment), in the order of the first event of each browser session. */
export function currentStates(states: readonly BrowserState[]): ReadonlyMap<string, BrowserState> {
  const byBrowser = new Map<string, BrowserState>();
  for (const state of states) {
    if (byBrowser.get(state.browser_id)?.state !== 'closed') byBrowser.set(state.browser_id, state);
  }
  return byBrowser;
}

/** The `browser_id` of the newest browser session that is not closed, else null. */
export function newestOpen(states: readonly BrowserState[]): string | null {
  const current = currentStates(states);
  for (let index = states.length - 1; index >= 0; index -= 1) {
    const id = states[index]!.browser_id;
    if (current.get(id)!.state !== 'closed') return id;
  }
  return null;
}

export interface BrowserStart {
  /** Time since the start began. */
  readonly elapsedMs: number;
  /** Time left to the 10 s target (never negative). */
  readonly remainingMs: number;
  /** The target has passed ("delayed"). */
  readonly overdue: boolean;
}

/** The countdown of a starting browser session; `at` is the event time, `now` the browser time. */
export function browserStart(state: BrowserState, at: number, now: number): BrowserStart {
  const since = state.opened_at === null ? at : parseTime(state.opened_at);
  const elapsedMs = Math.max(0, now - since);
  return {
    elapsedMs,
    remainingMs: Math.max(0, START_TARGET_MS - elapsedMs),
    overdue: elapsedMs > START_TARGET_MS,
  };
}
