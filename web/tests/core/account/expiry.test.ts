import { describe, expect, it } from 'vitest';
import { watchSessionExpiry } from '../../../src/core/account/expiry';
import { RENEW_BEFORE_MS } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { sessionRenewed } from '../../../src/core/store/slices/connection';
import { setSession } from '../../../src/core/store/slices/me';

/** Manual timers: `run()` fires the pending callback after moving the clock to its due time. */
function clock(start: number) {
  const state = { now: start, pending: null as null | { at: number; callback: () => void }, cleared: 0 };
  return {
    state,
    timers: {
      setTimeout: (callback: () => void, ms: number) => {
        state.pending = { at: state.now + ms, callback };
        return state.pending;
      },
      clearTimeout: (handle: unknown) => {
        if (state.pending === handle) state.pending = null;
        state.cleared += 1;
      },
    },
    now: () => state.now,
    run() {
      const pending = state.pending!;
      state.pending = null;
      state.now = pending.at;
      pending.callback();
    },
  };
}

describe('watchSessionExpiry', () => {
  it('notes expiring five minutes before and expired at the end, without any request', () => {
    const store = createStore();
    const c = clock(0);
    const stop = watchSessionExpiry(store, c.timers, c.now);
    expect(c.state.pending).toBeNull();
    setSession(store, 'u', { keyId: 'k', expiresAt: 3600_000 });
    expect(c.state.pending!.at).toBe(3600_000 - RENEW_BEFORE_MS);
    c.run();
    expect(store.getState().connection.session).toBe('expiring');
    expect(c.state.pending!.at).toBe(3600_000);
    c.run();
    expect(store.getState().connection.session).toBe('expired');
    expect(c.state.pending).toBeNull();
    // a renewed session schedules again
    sessionRenewed(store);
    setSession(store, 'u', { keyId: 'k2', expiresAt: 7200_000 });
    expect(c.state.pending!.at).toBe(7200_000 - RENEW_BEFORE_MS);
    stop();
    expect(c.state.pending).toBeNull();
    stop();
  });

  it('notes at once for a session that already ends soon, and uses the system timers by default', () => {
    const store = createStore();
    setSession(store, 'u', { keyId: 'k', expiresAt: Date.now() + 60_000 });
    const stop = watchSessionExpiry(store);
    expect(store.getState().connection.session).toBe('expiring');
    stop();
    const ended = createStore();
    setSession(ended, 'u', { keyId: 'k', expiresAt: 1 });
    watchSessionExpiry(ended)();
    expect(ended.getState().connection.session).toBe('expired');
    store.reset();
  });
});
