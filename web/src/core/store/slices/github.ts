// Slice `github`: the user's GitHub connection as `core/github/connection.ts` last verified it (`unknown` until it was
// loaded). Read by the settings section and by the composer's "GitHub pull request" chip (06-web.md sections 4.1, 8.1).
import type { GithubConnection } from '../../github/connection';
import type { Slice, Store } from '../store';

export type GithubState = GithubConnection | { readonly status: 'unknown' };

declare module '../store' {
  interface StoreState {
    github: GithubState;
  }
}

export const slice: Slice<'github'> = { name: 'github', initial: () => ({ status: 'unknown' }) };

export function setGithub(store: Store, connection: GithubConnection): void {
  store.update('github', () => connection);
}
