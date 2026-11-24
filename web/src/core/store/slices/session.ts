// Slice `session`: where the browser stands in the sign-in lifecycle (06-web.md sections 2.6, 3.4), the user's locale
// from `user_settings` (section 2.5), the org the screens work in and the
// `prf_missing` event of the last sign-in (section 3.3; the guidance screen is WEB-14's). Keys stay in `me`/`orgs`.
import type { Locale } from '../../i18n/locale';
import type { Slice, Store } from '../store';

/**
 * `starting`: the stored session is being restored; `anonymous`: no session; `locked`: a certified session key but
 * no identity in memory (after a reload: the passkey is needed again); `unlocked`: identity and scope keys loaded.
 */
export type SessionPhase = 'starting' | 'anonymous' | 'locked' | 'unlocked';

export interface SessionState {
  readonly phase: SessionPhase;
  /** `user_settings.locale` once known; null: the browser's language decides. */
  readonly locale: Locale | null;
  readonly activeOrg: string | null;
  /** The last sign-in returned no usable PRF output (`prf_missing`). */
  readonly prfMissing: boolean;
}

declare module '../store' {
  interface StoreState {
    session: SessionState;
  }
}

export const slice: Slice<'session'> = {
  name: 'session',
  initial: () => ({ phase: 'starting', locale: null, activeOrg: null, prfMissing: false }),
};

function patch(store: Store, values: Partial<SessionState>): void {
  store.update('session', (session) => {
    const changed = (Object.keys(values) as (keyof SessionState)[]).some((key) => session[key] !== values[key]);
    return changed ? { ...session, ...values } : session;
  });
}

export function setPhase(store: Store, phase: SessionPhase): void {
  patch(store, { phase });
}

export function setLocale(store: Store, locale: Locale | null): void {
  patch(store, { locale });
}

export function setActiveOrg(store: Store, org: string | null): void {
  patch(store, { activeOrg: org });
}

export function setPrfMissing(store: Store, missing: boolean): void {
  patch(store, { prfMissing: missing });
}
