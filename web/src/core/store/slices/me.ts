// Slice `me`: the signed-in user (`GET /v1/me`), the session's certificate metadata and the unlocked identity. The
// identity's private keys stay inside the `Identity` object (non-extractable `CryptoKey`s) and only in memory.
import type { Identity } from '../../crypto/keys';
import type { Slice, Store } from '../store';

export interface SessionMeta {
  readonly keyId: string;
  /** `expires_at` (Unix ms). */
  readonly expiresAt: number;
}

export interface MeState {
  readonly userId: string | null;
  /** The body of `GET /v1/me` as last received. */
  readonly profile: Readonly<Record<string, unknown>> | null;
  readonly session: SessionMeta | null;
  readonly identity: Identity | null;
}

declare module '../store' {
  interface StoreState {
    me: MeState;
  }
}

export const slice: Slice<'me'> = {
  name: 'me',
  initial: () => ({ userId: null, profile: null, session: null, identity: null }),
};

export function setSession(store: Store, userId: string, session: SessionMeta): void {
  store.update('me', (me) => ({ ...me, userId, session }));
}

export function setProfile(store: Store, profile: Readonly<Record<string, unknown>>): void {
  store.update('me', (me) => ({ ...me, profile }));
}

export function setIdentity(store: Store, identity: Identity | null): void {
  store.update('me', (me) => ({ ...me, identity }));
}
