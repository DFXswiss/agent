// Slice `pulls` (KOR-48, ADR-084): the pull-request links of workgroups and tasks as the browser opened them, and the
// fresh state of each pull request as a device read it.
//
// - `links`: one entry per link (`linkId(wg, epoch, tag)`): the hub's metadata (task, the device's session or the
//   person who linked it, time) and the pull request opened from the sealed body – null when it could not be opened
//   (`code`: `legacy` for a link without a body, else the crypto code); such a link shows "details unavailable".
// - `lists`: the loaded links of a workgroup (`listKey(wg, null)`) or of one task (`listKey(wg, task)`), newest
//   first, with the cursor of the next page and the load state.
// - `status`: per workgroup and pull request (`statusKey`): `checking` while a device request runs, `ready` with the
//   checked `pull_state` and the local time of the read, or `failed` with the code of the whole request. A status that
//   is not `ready` keeps the last checked state (`last`), so what was learnt from it (the head branch of the fix
//   feedback) survives a new read.
//
// The action functions and selectors live in `core/pulls/state.ts`: slices load with the first screen, the pull-request
// code only when it is shown (06-web.md section 12.2).
import type { PullRef } from '../../pulls/address';
import type { PullState } from '../../pulls/checks';
import type { Slice } from '../store';

export interface PullLink {
  readonly wg: string;
  readonly epoch: number;
  readonly tag: string;
  readonly taskId: string;
  /** The session whose device registered the link after `github.create_pull_request`; null for a person's link. */
  readonly sessionId: string | null;
  /** The person who linked the pull request by its address; null for a device's link. */
  readonly linkedBy: string | null;
  readonly createdAt: string;
  /** The opened pull request; null when the body is missing or could not be opened ("details unavailable"). */
  readonly pull: PullRef | null;
  /** Why the details are unavailable: `legacy` (no body) or the crypto code; null when opened. */
  readonly code: string | null;
}

export type LinkListStatus = 'loading' | 'ready' | 'error';

export interface LinkList {
  /** Link IDs newest first. */
  readonly ids: readonly string[];
  readonly nextCursor: string | null;
  readonly status: LinkListStatus;
  readonly code: string | null;
}

export type PullStatus =
  | { readonly status: 'checking'; readonly last?: PullState }
  | { readonly status: 'ready'; readonly state: PullState; readonly at: number }
  | { readonly status: 'failed'; readonly code: string; readonly last?: PullState };

export interface PullsState {
  readonly links: Readonly<Record<string, PullLink>>;
  readonly lists: Readonly<Record<string, LinkList>>;
  readonly status: Readonly<Record<string, PullStatus>>;
}

declare module '../store' {
  interface StoreState {
    pulls: PullsState;
  }
}

export const slice: Slice<'pulls'> = { name: 'pulls', initial: () => ({ links: {}, lists: {}, status: {} }) };
