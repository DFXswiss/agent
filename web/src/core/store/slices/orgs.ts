// Slice `orgs`: per org the verified membership log (rights projection at every seq), the scope keys the user holds
// (only in memory, as `Secret`s), the refused wraps and the blocking error state of 06-web.md section 11 (log rollback
// or fork, and every other log refusal: no further reads or writes in that org). Also the verified platform log.
import type { Secret } from '../../crypto/secret';
import type { Head } from '../../log/entries';
import type { VerifiedPlatformLog } from '../../log/platform';
import type { VerifiedLog } from '../../log/verify';
import type { Slice, Store } from '../store';

/** Evidence of a blocking log error, for support: the stored head, the head the hub served, the event's `lh`. */
export interface OrgBlock {
  readonly code: string;
  readonly stored: Head | null;
  readonly served: Head | null;
  readonly lh: number | null;
  /**
   * `outdated`: the log holds an entry type this bundle does not know (`log_type`), written by a newer version; the
   * UI offers a reload instead of a verification error (ADR-060, forward compatibility).
   */
  readonly reason?: 'outdated';
}

export interface OrgEntry {
  readonly id: string;
  readonly log: VerifiedLog | null;
  readonly blocked: OrgBlock | null;
  /** `epochKey(scope, scope_id, epoch)` → scope key. */
  readonly keys: ReadonlyMap<string, Secret>;
  /** Refused wraps by slot (`scope scope_id epoch recipient`) → code (`wrap_unauthorized`, `signature_invalid`, …). */
  readonly refusedWraps: ReadonlyMap<string, string>;
}

export interface OrgsState {
  readonly byId: Readonly<Record<string, OrgEntry>>;
  readonly platform: VerifiedPlatformLog | null;
}

declare module '../store' {
  interface StoreState {
    orgs: OrgsState;
  }
}

export const slice: Slice<'orgs'> = { name: 'orgs', initial: () => ({ byId: {}, platform: null }) };

const empty = (id: string): OrgEntry => ({ id, log: null, blocked: null, keys: new Map(), refusedWraps: new Map() });

function updateOrg(store: Store, org: string, update: (entry: OrgEntry) => OrgEntry): void {
  store.update('orgs', (orgs) => ({ ...orgs, byId: { ...orgs.byId, [org]: update(orgs.byId[org] ?? empty(org)) } }));
}

/** The org entry (an empty one for an org not seen yet). */
export function orgEntry(orgs: OrgsState, org: string): OrgEntry {
  return orgs.byId[org] ?? empty(org);
}

/** Makes the org known to the store (e.g. from `GET /v1/me`). */
export function ensureOrg(store: Store, org: string): void {
  if (store.getState().orgs.byId[org] !== undefined) return;
  updateOrg(store, org, (entry) => entry);
}

export function setLog(store: Store, org: string, log: VerifiedLog): void {
  updateOrg(store, org, (entry) => ({ ...entry, log }));
}

/**
 * The blocking error state; the first block stays (its evidence is what support needs). A refusal for an unknown
 * entry type (`log_type`) is marked `outdated`.
 */
export function blockOrg(store: Store, org: string, block: Omit<OrgBlock, 'reason'>): void {
  const blocked: OrgBlock = block.code === 'log_type' ? { ...block, reason: 'outdated' } : block;
  updateOrg(store, org, (entry) => (entry.blocked === null ? { ...entry, blocked } : entry));
}

export function addKeys(store: Store, org: string, keys: ReadonlyMap<string, Secret>): void {
  if (keys.size === 0) return;
  updateOrg(store, org, (entry) => ({ ...entry, keys: new Map([...entry.keys, ...keys]) }));
}

/**
 * Records the outcome of a wrap check: `refused` slots with their codes; `accepted` slots leave the refused set (a
 * wrap refused before the log refresh that entitles its wrapper is accepted later).
 */
export function recordWraps(
  store: Store,
  org: string,
  refused: ReadonlyMap<string, string>,
  accepted: Iterable<string>,
): void {
  const known = orgEntry(store.getState().orgs, org).refusedWraps;
  const next = new Map(known);
  for (const slot of accepted) next.delete(slot);
  for (const [slot, code] of refused) next.set(slot, code);
  const same = next.size === known.size && [...next].every(([slot, code]) => known.get(slot) === code);
  if (same) return;
  updateOrg(store, org, (entry) => ({ ...entry, refusedWraps: next }));
}

export function setPlatform(store: Store, platform: VerifiedPlatformLog): void {
  store.update('orgs', (orgs) => ({ ...orgs, platform }));
}

/**
 * The `wrap_unauthorized` counter of an org (06-web.md section 11, job 2): distinct wraps refused by the signature or
 * W1-W2 check (wraps that verified but could not be opened, `decrypt_failed`, are kept in `refusedWraps` only).
 */
export function wrapUnauthorized(entry: OrgEntry): number {
  return [...entry.refusedWraps.values()].filter((code) => code !== 'decrypt_failed').length;
}
