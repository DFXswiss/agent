// What a pull-request row shows and offers (KOR-48, ADR-084 sections 2 and 3), from the link and the status a device
// read (`status.ts`):
//
// - the state: `open`, `draft`, `merged` or `closed` (a merged pull request is closed at GitHub; it shows "merged");
// - for an open or draft pull request the conflict state: `conflict` (GitHub's `mergeable: false` – alone; a draft
//   reports `mergeable_state: "draft"` even with a conflict, so `mergeable_state` is never used), `checking`
//   (`mergeable: null`, never shown as "no conflict") or `clean` (`mergeable: true`); closed and merged pull requests
//   show their state only;
// - a fork: the head branch lives in another repository (`head_repo` differs, or GitHub names none) – "Fix conflicts" is
//   not available there (not supported, ADR-084 section 4);
// - the link to GitHub: the device's `url` when it is https, else the pull request's address built from the repository
//   and number (https only, never a link the session could choose);
// - `canFix`: only for a conflict of a pull request in the same repository and only for a user who may write prompts
//   to the task (`task:write`).
import { httpsUrl } from '../operations/view';
import type { PullCode } from './checks';
import type { PullStatus } from './state';
import { type PullRef, pullUrl } from './address';

export type PullDisplay =
  | { readonly kind: 'unchecked' }
  | { readonly kind: 'checking' }
  | { readonly kind: 'failed'; readonly code: string }
  | { readonly kind: 'missing'; readonly code: PullCode }
  | {
      readonly kind: 'found';
      readonly state: 'open' | 'draft' | 'merged' | 'closed';
      readonly conflict: 'conflict' | 'checking' | 'clean' | null;
      readonly fork: boolean;
      readonly title: string;
      readonly headRef: string;
      readonly url: string;
    };

/** The display of a pull request from its status (module comment). */
export function displayOf(ref: PullRef, status: PullStatus | null): PullDisplay {
  if (status === null) return { kind: 'unchecked' };
  if (status.status === 'checking') return { kind: 'checking' };
  if (status.status === 'failed') return { kind: 'failed', code: status.code };
  const read = status.state;
  if (!read.found) return { kind: 'missing', code: read.code };
  const state = read.merged ? 'merged' : read.state === 'closed' ? 'closed' : read.draft ? 'draft' : 'open';
  const live = state === 'open' || state === 'draft';
  const conflict = !live
    ? null
    : read.mergeable === false
      ? 'conflict'
      : read.mergeable === null
        ? 'checking'
        : 'clean';
  return {
    kind: 'found',
    state,
    conflict,
    fork: read.head_repo === null || read.head_repo.toLowerCase() !== read.repo.toLowerCase(),
    title: read.title,
    headRef: read.head_ref,
    url: httpsUrl(read.url) ?? pullUrl(ref),
  };
}

/** Whether "Fix conflicts" is offered (module comment). */
export const canFix = (display: PullDisplay, writer: boolean): boolean =>
  writer && display.kind === 'found' && display.conflict === 'conflict' && !display.fork;

/** `aria-current` of a workgroup level in the pull-request view: `page` for the shown workgroup. */
export const levelCurrent = (id: string, shown: string): 'page' | undefined => (id === shown ? 'page' : undefined);
