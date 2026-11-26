// Slice `support` (ADR-096, KOR-56): the release of workgroups to DFX support.
//
// - `orgs`: per organization of the user its active releases and the current support persons with their display names
//   (`GET /v1/orgs/{org}/support`; the names are the operator's, the person's keys come from the verified platform
//   log, never from here);
// - `marks`: the ID mark `#XXXX` of a user (`core/community/mark.ts`), computed once per user, so that a name the hub
//   gives cannot hide which certified person acted;
// - `area`: the support area of a support person (`GET /v1/support/releases`);
// - `opened`: the organizations the support area verified (membership log from `seq` 0 against the platform log) and
//   the workgroup keys addressed to the person – only in memory, like every key.
import type { Secret } from '../../crypto/secret';
import type { VerifiedLog } from '../../log/verify';
import type { Slice, Store } from '../store';

export type Access = 'read' | 'write';

/** An active release of a workgroup of the user's organization. */
export interface OrgRelease {
  readonly wg_id: string;
  readonly access: Access;
  readonly granted_seq: number;
  readonly granted_at: string;
}

export interface OrgSupport {
  readonly status: 'loading' | 'ready' | 'error';
  readonly code: string | null;
  readonly releases: readonly OrgRelease[];
  /** Current support persons by user ID: the display name the operator set, null without one. */
  readonly people: Readonly<Record<string, string | null>>;
}

/** An active release as a support person sees it. */
export interface SupportRelease extends OrgRelease {
  readonly org_id: string;
}

export interface SupportArea {
  /** `none`: the hub does not know the user as a support person (`404`). */
  readonly status: 'idle' | 'loading' | 'ready' | 'none' | 'error';
  readonly code: string | null;
  readonly releases: readonly SupportRelease[];
}

/** An organization the support area verified, with the keys of its released workgroups opened for the person. */
export interface OpenedOrg {
  readonly log: VerifiedLog;
  /** `epochKey(scope, scope_id, epoch)` → key. */
  readonly keys: ReadonlyMap<string, Secret>;
}

export interface SupportState {
  readonly orgs: Readonly<Record<string, OrgSupport>>;
  readonly marks: Readonly<Record<string, string>>;
  readonly area: SupportArea;
  readonly opened: Readonly<Record<string, OpenedOrg>>;
}

declare module '../store' {
  interface StoreState {
    support: SupportState;
  }
}

export const slice: Slice<'support'> = {
  name: 'support',
  initial: () => ({ orgs: {}, marks: {}, area: { status: 'idle', code: null, releases: [] }, opened: {} }),
};

export function setOrgSupport(store: Store, org: string, value: OrgSupport): void {
  store.update('support', (support) => ({ ...support, orgs: { ...support.orgs, [org]: value } }));
}

export function setMarks(store: Store, marks: Readonly<Record<string, string>>): void {
  store.update('support', (support) => ({ ...support, marks: { ...support.marks, ...marks } }));
}

export function setArea(store: Store, area: SupportArea): void {
  store.update('support', (support) => ({ ...support, area }));
}

export function setOpened(store: Store, org: string, opened: OpenedOrg): void {
  store.update('support', (support) => ({ ...support, opened: { ...support.opened, [org]: opened } }));
}
