// The append flow of every membership change made in the browser (06-web.md sections 7.1-7.3, 11 job 4; 04-crypto.md
// sections 4.7, 5.10; 03-api.md section 4.3). Exported for the device screen (WEB-07: unassign and unpair use
// `submitBatch` with their own planner).
//
// `submitBatch(context, org, planner)`:
// 1. the planner looks at the verified log and returns the requested entries (or null: nothing to do any more, e.g.
//    the member was removed by another admin meanwhile);
// 2. `buildBatch` adds one `rotate` per element of R with fresh keys and the wraps of every new epoch for all remaining
//    recipients (and the current epochs for newly entitled principals, older ones with `history`), and checks the
//    result against the hub's rules; the planner may add events sealed against the batch result (e.g. `wg.profile` of
//    a new workgroup, at `lh` = the batch's last entry);
// 3. one `POST /v1/orgs/{org}/log`; the answer must name the head the batch produces;
// 4. on `seq_conflict` (another browser appended first) the log is re-read and steps 1-3 run again, at most
//    `BATCH_ATTEMPTS` times in total; any other refusal ends the flow with its code.
// After success the verified log of the batch replaces the stored one and the new keys of scopes the user reads are
// kept in memory. `progress` reports a new attempt after `seq_conflict` (the dialog otherwise shows only "Saving …";
// key handling stays out of sight, ADR-058 section 4).
import { formatTime } from '../encoding/time';
import { CryptoError } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import type { KeyRing } from '../log/catchup';
import type { LogFields } from '../log/entries';
import type { PlatformState } from '../log/platform';
import { buildBatch, type BuiltBatch } from '../log/rotation';
import { epochKey } from '../log/state';
import type { VerifiedLog } from '../log/verify';
import { HubError, type SignedClient } from '../net/signedFetch';
import { isRecord } from '../registry';
import { addKeys, orgEntry, setLog } from '../store/slices/orgs';
import type { Store } from '../store/store';

/** Attempts of one change that meets `seq_conflict` (the log is re-read and the batch rebuilt each time). */
export const BATCH_ATTEMPTS = 3;

/** A refused change with a catalog code (`forbidden`, `last_admin`, `epoch_stale`, hub and crypto codes). */
export class OrgActionError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = 'OrgActionError';
    this.code = code;
  }
}

/** The catalog code of any failure of a change. */
export function actionErrorCode(error: unknown): string {
  if (error instanceof OrgActionError || error instanceof HubError || error instanceof CryptoError) return error.code;
  return 'unavailable';
}

export function refuse(code: string): never {
  throw new OrgActionError(code);
}

/** What the flows need of the signed-in browser (`AppSession.tasks` provides it; jobs pass their own). */
export interface OrgContext {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  /** Re-reads and verifies the org's log (after `seq_conflict`). */
  readonly refreshLog: (org: string) => Promise<unknown>;
  readonly now: () => number;
}

/** The signed-in user with the unlocked identity and the verified log of an org. */
export interface Actor {
  readonly userId: string;
  readonly identity: Identity;
  readonly log: VerifiedLog;
}

/** The actor of `org`; a blocked org refuses every write (its code), an unverified one `log_empty`. */
export function actorOf(store: Store, org: string): Actor {
  const { userId, identity } = store.getState().me;
  const entry = orgEntry(store.getState().orgs, org);
  if (userId === null || identity === null) refuse('unauthorized');
  if (entry.blocked !== null) refuse(entry.blocked.code);
  if (entry.log === null) refuse('log_empty');
  return { userId, identity, log: entry.log };
}

/** The verified platform state (null until the log job verified the platform log). */
export function verifiedPlatform(store: Store): PlatformState | null {
  return store.getState().orgs.platform?.state ?? null;
}

/** The scope keys the user holds in `org`. */
export function keyRing(store: Store, org: string): KeyRing {
  const keys = orgEntry(store.getState().orgs, org).keys;
  return (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null;
}

/**
 * Runs `attempt` with the current actor; on `seq_conflict` re-reads the log and runs it again (at most
 * `BATCH_ATTEMPTS` times). `onRetry` is told the number of the next attempt.
 */
export async function withLogRetry<T>(
  context: OrgContext,
  org: string,
  attempt: (actor: Actor, number: number) => Promise<T>,
  onRetry?: (number: number) => void,
): Promise<T> {
  for (let number = 1; ; number += 1) {
    try {
      return await attempt(actorOf(context.store, org), number);
    } catch (error) {
      if (!(error instanceof HubError) || error.code !== 'seq_conflict' || number >= BATCH_ATTEMPTS) throw error;
    }
    onRetry?.(number + 1);
    await context.refreshLog(org);
  }
}

/** What a planner asks for. */
export interface BatchPlan {
  readonly entries: readonly LogFields[];
  /** `grant` entries: also wrap the older epochs of the workgroup ("also give access to history"). */
  readonly history?: boolean;
  /** Events sent with the batch, built against its result by the actor (keys include the new ones). */
  readonly events?: (built: BuiltBatch, keys: KeyRing, actor: Actor) => Promise<unknown[]>;
}

/** Looks at the verified log and returns the change, or null if nothing is left to do. */
export type Planner = (log: VerifiedLog, userId: string) => BatchPlan | null;

/** A progress report of `submitBatch`. */
export interface SubmitProgress {
  readonly step: 'retry';
  readonly attempt: number;
}

/** The appended batch, or null if the planner found nothing to do. */
export interface Submitted {
  readonly built: BuiltBatch | null;
  readonly attempts: number;
}

/** The answer of `POST /v1/orgs/{org}/log` must name the head the batch produces. */
export function requireHead(answer: unknown, log: VerifiedLog): void {
  const head = isRecord(answer) && isRecord(answer.head) ? answer.head : null;
  if (head?.seq !== log.head.seq || head.hash !== log.head.hash) throw new HubError('unavailable', 200);
}

/** Stores the verified log after an accepted batch and the new keys of the scopes the user reads. */
export function install(store: Store, org: string, userId: string, built: BuiltBatch): void {
  setLog(store, org, built.log);
  const state = built.log.state;
  const keys = new Map<string, Secret>();
  for (const key of built.newKeys) {
    if (state.canRead(userId, key.scope, key.scope_id)) keys.set(epochKey(key.scope, key.scope_id, key.epoch), key.key);
  }
  addKeys(store, org, keys);
}

/** The flow described at the top of this file. */
export async function submitBatch(
  context: OrgContext,
  org: string,
  planner: Planner,
  progress?: (step: SubmitProgress) => void,
): Promise<Submitted> {
  let attempts = 0;
  const built = await withLogRetry(
    context,
    org,
    async (actor, number) => {
      attempts = number;
      const plan = planner(actor.log, actor.userId);
      if (plan === null) return null;
      const held = keyRing(context.store, org);
      const result = await buildBatch({
        log: actor.log,
        author: { id: actor.userId, identity: actor.identity },
        entries: plan.entries,
        keys: held,
        history: plan.history === true,
        platform: verifiedPlatform(context.store),
        ts: formatTime(context.now()),
      });
      const fresh = new Map(result.newKeys.map((key) => [epochKey(key.scope, key.scope_id, key.epoch), key.key]));
      const keys: KeyRing = (scope, scopeId, epoch) =>
        fresh.get(epochKey(scope, scopeId, epoch)) ?? held(scope, scopeId, epoch);
      const events = plan.events === undefined ? [] : await plan.events(result, keys, actor);
      const answer = await context.client.request('POST', `/v1/orgs/${encodeURIComponent(org)}/log`, {
        body: { ...result.body, events },
      });
      requireHead(answer, result.log);
      install(context.store, org, actor.userId, result);
      return result;
    },
    (attempt) => progress?.({ step: 'retry', attempt }),
  );
  return { built, attempts };
}
