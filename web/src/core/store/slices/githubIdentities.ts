// Slice `githubIdentities` (ADR-078, 06-web.md section 8.1): the user's GitHub identities as
// `core/github/identities.ts` last loaded them – own records verified and opened (login, name, source, scopes,
// whether a signing key exists; never a token or a key seed), company records by metadata only (no browser can open
// them, KM-35) – with the defaults of the organizations and workgroups (`unknown` until loaded), and the logins of
// company records this browser stored in this sign-in (`knownLogins`, record ID → login; reset on sign-out). Read by
// the GitHub settings, the company accounts screen, the workgroup settings, the task's identity choice and the
// Operations tab.
import type { GithubDefault, GithubIdentity } from '../../github/identities';
import type { Slice, Store } from '../store';

export interface GithubIdentitiesState {
  readonly status: 'unknown' | 'loaded';
  readonly identities: readonly GithubIdentity[];
  readonly defaults: readonly GithubDefault[];
  readonly knownLogins: Readonly<Record<string, string>>;
}

declare module '../store' {
  interface StoreState {
    githubIdentities: GithubIdentitiesState;
  }
}

export const slice: Slice<'githubIdentities'> = {
  name: 'githubIdentities',
  initial: () => ({ status: 'unknown', identities: [], defaults: [], knownLogins: {} }),
};

export function setGithubIdentities(
  store: Store,
  identities: readonly GithubIdentity[],
  defaults: readonly GithubDefault[],
): void {
  store.update('githubIdentities', (state) => ({ ...state, status: 'loaded', identities, defaults }));
}

/** The login of a company record this browser just stored. */
export function noteCompanyLogin(store: Store, id: string, login: string): void {
  store.update('githubIdentities', (state) => ({ ...state, knownLogins: { ...state.knownLogins, [id]: login } }));
}
