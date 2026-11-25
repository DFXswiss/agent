// The browser sessions of a session from its `browser.state` events: the selector (task, session, kind, verified,
// valid; memoized), the current state per browser session, the newest open one and the countdown of a start.
import { describe, expect, it } from 'vitest';
import { browserStart, currentStates, newestOpen, selectBrowserStates } from '../../../src/core/browser/sessions';
import { parseBrowserState, type BrowserState } from '../../../src/core/browser/shape';
import { createStore } from '../../../src/core/store';
import { putEvents } from '../../../src/core/store/slices/events';
import { BROWSER, BROWSER2, OTHER, read, state, WG, TASK } from './world';

const body = (overrides: Record<string, unknown>) => parseBrowserState(state(overrides))!;

describe('selectBrowserStates', () => {
  it('keeps the valid verified states of the session in scope order and is memoized on the scope', () => {
    const store = createStore();
    const select = selectBrowserStates(WG, TASK, read(1).header!.session as string);
    expect(select(store.getState())).toEqual([]);
    putEvents(store, WG, [
      read(4, { kind: 'browser.state', body: state({ state: 'closed', reason: 'browser_idle' }) }),
      read(2, { kind: 'browser.state', body: state({ state: 'starting' }) }),
      read(3, { kind: 'browser.state', body: { broken: true } }),
      read(5, { kind: 'browser.state', session: OTHER, body: state() }),
      read(6, { kind: 'browser.state', task: 'other-task', body: state() }),
      read(7, { kind: 'browser.state', status: 'unverifiable' }),
      read(8),
    ]);
    const states = select(store.getState());
    expect(states.map((item) => item.state)).toEqual(['starting', 'closed']);
    expect(select(store.getState())).toBe(states);
  });
});

describe('current and newest open browser session', () => {
  it('keeps closed as closed and finds the newest not closed one', () => {
    const late = [body({ state: 'closed', reason: 'browser_closed' }), body({ state: 'open' })];
    expect(currentStates(late).get(BROWSER)!.state).toBe('closed');
    expect(newestOpen(late)).toBeNull();
    expect(newestOpen([])).toBeNull();
    const two: BrowserState[] = [
      body({ state: 'open' }),
      body({ browser_id: BROWSER2, state: 'starting' }),
      body({ browser_id: BROWSER2, state: 'restarted', reason: 'vm_lost' }),
    ];
    expect(newestOpen(two)).toBe(BROWSER2);
    expect(newestOpen([...two, body({ browser_id: BROWSER2, state: 'closed' })])).toBe(BROWSER);
  });
});

describe('browserStart', () => {
  const at = Date.parse('2026-11-25T12:00:00.000Z');

  it('counts down from opened_at, else from the event time, and is delayed after 10 s', () => {
    expect(browserStart(body({ state: 'starting' }), 0, at + 4000)).toEqual({
      elapsedMs: 4000,
      remainingMs: 6000,
      overdue: false,
    });
    expect(browserStart(body({ state: 'starting', opened_at: null }), at, at + 12_000)).toEqual({
      elapsedMs: 12_000,
      remainingMs: 0,
      overdue: true,
    });
    expect(browserStart(body({ state: 'starting' }), 0, at - 50).elapsedMs).toBe(0);
  });
});
