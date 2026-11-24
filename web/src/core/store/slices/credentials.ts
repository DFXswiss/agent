// Slice `credentials` (06-web.md sections 8.2 and 11 job 5): the user's credential records as `core/credentials`
// last opened them (`unknown` until loaded) – kind, version, epoch, update time and the non-secret summary only (never
// a token, key or password) – and the toasts of the credential re-wrap job (a rotation runs silently; one blocked by
// records the browser cannot open leaves a notice until it is dismissed; at most `MAX_NOTICES`, oldest dropped).
import type { CredentialKind, Summary } from '../../credentials/records';
import type { RotationResult } from '../../jobs/credential-rewrap';
import type { Slice, Store } from '../store';

export interface CredentialEntry {
  readonly kind: CredentialKind;
  readonly version: number;
  readonly epoch: number;
  readonly updatedAt: string;
  readonly status: 'verified' | 'unverifiable';
  readonly summary: Summary | null;
  /** The code of an unverifiable record. */
  readonly code: string | null;
}

export interface CredentialRotationNotice {
  readonly id: number;
  /** A rotation blocked by unreadable records (the user deletes or replaces them). */
  readonly result: Extract<RotationResult, { status: 'blocked' }>;
  readonly at: number;
}

export interface CredentialsState {
  readonly entries: readonly CredentialEntry[] | null;
  readonly notices: readonly CredentialRotationNotice[];
  readonly next: number;
}

declare module '../store' {
  interface StoreState {
    credentials: CredentialsState;
  }
}

export const MAX_NOTICES = 3;

export const slice: Slice<'credentials'> = {
  name: 'credentials',
  initial: () => ({ entries: null, notices: [], next: 1 }),
};

export function setCredentials(store: Store, entries: readonly CredentialEntry[]): void {
  store.update('credentials', (state) => ({ ...state, entries }));
}

export function noteCredentialRotation(store: Store, notice: Omit<CredentialRotationNotice, 'id'>): void {
  store.update('credentials', (state) => ({
    ...state,
    notices: [...state.notices, { ...notice, id: state.next }].slice(-MAX_NOTICES),
    next: state.next + 1,
  }));
}

export function dismissCredentialRotation(store: Store, id: number): void {
  store.update('credentials', (state) =>
    state.notices.some((notice) => notice.id === id)
      ? { ...state, notices: state.notices.filter((notice) => notice.id !== id) }
      : state,
  );
}
