// Job 6 of 06-web.md section 11: self-wraps after `accept` (04-crypto.md section 5.2 steps 2 and 3, W1 of section
// 6.3). The invitation screen queues the package keys of the joined org for the joining user (`queueSelfWraps`); the
// job stores a wrap of every queued key for the user's own identity, signed by itself, so that a second browser of
// the user can unlock the org without waiting for another member's catch-up. Keys the user already has a wrap for
// (the `accept` batch carries the self-wraps of the current epochs) and keys the verified log does not let the user
// wrap for itself (W1: the user reads the scope now, the epoch exists) are skipped. The keys stay queued until a run
// with the verified log containing the user's `accept` sent the wraps (`POST /v1/wraps`).
//
// The queue may also hold the display name of an `accept` that went out without `member.profile` (the package lacked
// the current org key, decision WEB-08 8): once the user holds the current org key (catch-up of another member), the
// job sends the event (`POST /v1/events`). A queue belongs to the user who joined: a run for anybody else (sign-out
// and another sign-in in the same tab) drops it unused; a run that started before a sign-out sends nothing.
import { memberProfile } from '../account/join';
import { buildWraps } from '../log/catchup';
import { excludedFor } from '../log/platform';
import { epochKey, ORG } from '../log/state';
import { mayWrap, slotKey, type WrapSlot } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import { keyRing, verifiedPlatform } from '../org/batch';
import { orgEntry } from '../store/slices/orgs';
import type { Store } from '../store/store';
import type { VerifiedLog } from '../log/verify';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';

/** A scope key of an invitation package. */
export interface QueuedKey {
  readonly scope: string;
  readonly scope_id: string;
  readonly epoch: number;
  readonly key: Secret;
}

/** What is left to do for a joined org. */
export interface SelfWrapQueue {
  readonly userId: string;
  readonly keys: readonly QueuedKey[];
  /** The display name of a `member.profile` still to send, or null. */
  readonly profile: string | null;
}

const queues = new WeakMap<Store, Map<string, SelfWrapQueue>>();

function queueOf(store: Store): Map<string, SelfWrapQueue> {
  let queue = queues.get(store);
  if (queue === undefined) {
    queue = new Map();
    queues.set(store, queue);
  }
  return queue;
}

/** Queues the package keys (and a pending profile) of `org` for `userId` (replaces an earlier queue of the org). */
export function queueSelfWraps(
  store: Store,
  org: string,
  userId: string,
  keys: readonly QueuedKey[],
  profile: string | null = null,
): void {
  queueOf(store).set(org, { userId, keys: [...keys], profile });
}

/** The queue of `org`, or null. */
export function queuedSelfWraps(store: Store, org: string): SelfWrapQueue | null {
  return queues.get(store)?.get(org) ?? null;
}

/** The slots the user already has a wrap for in the scopes of `keys` (`GET /v1/wraps` per scope). */
async function ownSlots(context: JobContext, userId: string, keys: readonly QueuedKey[]): Promise<Set<string>> {
  const scopes = new Map(keys.map((key) => [`${key.scope} ${key.scope_id}`, key]));
  const own = new Set<string>();
  for (const { scope, scope_id: scopeId } of scopes.values()) {
    const target = `/v1/wraps?scope=${encodeURIComponent(scope)}&scope_id=${encodeURIComponent(scopeId)}`;
    const body = await context.client.request('GET', target);
    if (!isRecord(body) || !Array.isArray(body.wraps)) throw new HubError('unavailable', 200);
    for (const wrap of body.wraps.filter(isRecord)) {
      if (wrap.recipient_id !== userId || wrap.scope !== scope || wrap.scope_id !== scopeId) continue;
      own.add(slotKey({ scope, scope_id: scopeId, epoch: wrap.epoch as number, recipient: userId }));
    }
  }
  return own;
}

/** The self-wraps of the queued keys the user has no wrap for and W1 allows. */
async function selfWraps(context: JobContext, log: VerifiedLog, identity: Identity, queued: SelfWrapQueue) {
  const { userId, keys } = queued;
  if (keys.length === 0) return [];
  const excluded = excludedFor(verifiedPlatform(context.store), log.state);
  const own = await ownSlots(context, userId, keys);
  const slots: WrapSlot[] = keys
    .map((key) => ({ scope: key.scope, scope_id: key.scope_id, epoch: key.epoch, recipient: userId }))
    .filter((slot) => !own.has(slotKey(slot)) && mayWrap(log, slot, userId, excluded));
  const ring = new Map(keys.map((key) => [epochKey(key.scope, key.scope_id, key.epoch), key.key]));
  // every slot comes from a queued key
  return buildWraps(identity, slots, (scope, id, epoch) => ring.get(epochKey(scope, id, epoch))!, [log.state]);
}

/** One run for `org`; returns the number of wraps and events sent. */
export async function storeSelfWraps(context: JobContext, run: JobRun): Promise<number> {
  const { store } = context;
  const queued = queuedSelfWraps(store, run.org);
  const { userId, identity } = store.getState().me;
  const log = orgEntry(store.getState().orgs, run.org).log;
  if (queued === null || userId === null || identity === null) return 0;
  const queue = queueOf(store);
  if (queued.userId !== userId) {
    queue.delete(run.org);
    return 0;
  }
  if (log?.state.member(userId) == null) return 0;
  const wraps = await selfWraps(context, log, identity, queued);
  const epoch = log.state.epoch(ORG, run.org);
  const orgKey = queued.profile === null ? null : keyRing(store, run.org)(ORG, run.org, epoch);
  const joiner = { userId, identity };
  // (statements, not a conditional expression with `await`: V8 miscounts the code after such an expression)
  let event: unknown = null;
  if (orgKey !== null) {
    event = await memberProfile(run.org, epoch, orgKey, joiner, queued.profile!, log.head.seq, context.now());
  }
  if (!current(context, run)) return 0;
  if (wraps.length > 0) await context.client.request('POST', '/v1/wraps', { body: { wraps } });
  if (event !== null) await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  if (queue.get(run.org) === queued) {
    const profile = event === null ? queued.profile : null;
    if (profile === null) queue.delete(run.org);
    else queue.set(run.org, { userId, keys: [], profile });
  }
  return wraps.length + (event === null ? 0 : 1);
}

export const job: JobRegistration = {
  name: 'self-wraps',
  order: 35,
  triggers: ['login', 'log', 'interval'],
  run: async (context: JobContext, run: JobRun) => {
    await storeSelfWraps(context, run);
  },
};
