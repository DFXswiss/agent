// Slice `connection`: the live channel's state for the connection banner (06-web.md section 12.5: Live,
// Reconnecting… (n s), Offline) and the session notice for the renewal prompt (section 2.6). `round` counts the
// channel's `hello` and `resync` frames: the hub replays events after a reconnect, but no row frames (`task`, `session`,
// `notification`, usage), and a `resync` says it dropped frames. Every screen that shows such rows loads them again for
// each new value (KOR-11, AUD-02 NA-05; `ui/hooks/useLiveReload.ts`) – push is a hint, pull is the truth.
import type { LiveState, LiveStatus } from '../../net/sse';
import type { SessionNotice } from '../../net/signedFetch';
import type { Slice, Store } from '../store';

export interface ConnectionState {
  readonly status: LiveStatus;
  readonly retryAt: number | null;
  readonly attempt: number;
  readonly error: string | null;
  /** `navigator.onLine` as reported by the UI. */
  readonly online: boolean;
  /** `ok`, or `expiring` (< 5 min left) / `expired` (`401 session_expired`). */
  readonly session: 'ok' | SessionNotice;
  /** The number of `hello` and `resync` frames of the live channel so far (module comment). */
  readonly round: number;
}

declare module '../store' {
  interface StoreState {
    connection: ConnectionState;
  }
}

export const slice: Slice<'connection'> = {
  name: 'connection',
  initial: () => ({ status: 'idle', retryAt: null, attempt: 0, error: null, online: true, session: 'ok', round: 0 }),
};

export function setLiveState(store: Store, live: LiveState): void {
  store.update('connection', (connection) => ({ ...connection, ...live }));
}

export function setOnline(store: Store, online: boolean): void {
  store.update('connection', (connection) => (connection.online === online ? connection : { ...connection, online }));
}

/** A session notice of the signed client; `expired` is never downgraded to `expiring`. */
export function noteSession(store: Store, notice: SessionNotice): void {
  store.update('connection', (connection) => {
    if (connection.session === notice || connection.session === 'expired') return connection;
    return { ...connection, session: notice };
  });
}

/** After a renewed sign-in (new session key). */
export function sessionRenewed(store: Store): void {
  store.update('connection', (connection) =>
    connection.session === 'ok' ? connection : { ...connection, session: 'ok' },
  );
}

export type Banner =
  | { readonly kind: 'live' }
  | { readonly kind: 'reconnecting'; readonly seconds: number }
  | { readonly kind: 'offline' }
  | { readonly kind: 'connecting' }
  | { readonly kind: 'none' };

/** The banner of section 12.5 at time `now` (offline wins; "live" only after the cursors were pulled). */
export function banner(connection: ConnectionState, now: number): Banner {
  if (!connection.online || connection.status === 'offline') return { kind: 'offline' };
  if (connection.status === 'live') return { kind: 'live' };
  if (connection.status === 'reconnecting') {
    return { kind: 'reconnecting', seconds: Math.max(0, Math.ceil(((connection.retryAt ?? now) - now) / 1000)) };
  }
  if (connection.status === 'connecting' || connection.status === 'syncing') return { kind: 'connecting' };
  return { kind: 'none' };
}

const nextRound = (store: Store) =>
  store.update('connection', (connection) => ({ ...connection, round: connection.round + 1 }));

/** The frames this slice applies (`core/net/frames.ts`): `hello` (every connect and reconnect) and `resync`. */
export const frames: Readonly<Record<string, (store: Store) => void>> = { hello: nextRound, resync: nextRound };
