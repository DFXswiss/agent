// Session renewal reminder (06-web.md section 11 job 7, section 2.6): independent of requests, the `connection` slice
// gets the notice `expiring` five minutes before the certificate's `expires_at` and `expired` at it, so the
// "Sign in again" prompt also appears for a user who only reads. A renewed session (new `expires_at`) re-schedules.
import { RENEW_BEFORE_MS } from '../net/signedFetch';
import { noteSession } from '../store/slices/connection';
import { subscribe, type Store } from '../store/store';

export interface ExpiryTimers {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const SYSTEM: ExpiryTimers = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** Watches the session of `store`; returns the function that stops watching. */
export function watchSessionExpiry(store: Store, timers: ExpiryTimers = SYSTEM, now: () => number = Date.now) {
  let timer: unknown = null;
  const schedule = () => {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    const session = store.getState().me.session;
    if (session === null) return;
    const left = session.expiresAt - now();
    if (left <= 0) return noteSession(store, 'expired');
    if (left <= RENEW_BEFORE_MS) noteSession(store, 'expiring');
    timer = timers.setTimeout(schedule, left <= RENEW_BEFORE_MS ? left : left - RENEW_BEFORE_MS);
  };
  const unsubscribe = subscribe(store, (state) => state.me.session, schedule);
  schedule();
  return () => {
    unsubscribe();
    if (timer !== null) timers.clearTimeout(timer);
  };
}
