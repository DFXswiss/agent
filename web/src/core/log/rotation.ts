// The rotation obligation R (04-crypto.md section 4.7), the atomic append batch of `POST /v1/orgs/{org}/log` and
// `POST /v1/orgs` (03-api.md section 4.3, 04-crypto.md section 5.10) and its builder (06-web.md section 7.1,
// section 11 job 4). TypeScript twin of `agent_hub.log.rotation` and `agent_hub.log.batch`.
//
// `checkBatch` runs, in this order:
// 1. L1-L8 for every entry (`appendEntries`; a wrong first `seq` is `seq_conflict`);
// 2. the rotation obligation: R = the union of `R(before, after, subject)` over the entries, except a member that
//    leaves by itself or an admin that demotes itself (it cannot rotate what it no longer reads; its R becomes
//    `OrgState.pending`, ADR-058 section 3). If R is not empty the batch MUST contain exactly one
//    `rotate` per element of R and no other `rotate`, each after the last entry contributing to R
//    (`rotation_required`);
// 3. W3 for every wrap of the batch, created by `wrappedBy`, which must be active after the batch;
// 4. distribution of every epoch the batch creates and that is current at its end (`genesis`, `wg_create`,
//    `rotate`): a wrap for every entitled recipient (`rotation_required`);
// 5. `admit` (ADR-060): wraps for the admitted user of the current org epoch and of the current epoch of the default
//    workgroup (`log_admit`); further workgroups may follow by catch-up.
//
// `buildBatch` produces exactly such a batch from the verified log: the requested entries, one `rotate` per element
// of R with a fresh key, wraps of every new epoch for all remaining recipients and wraps of the current epochs for
// every principal the batch newly entitles (grant, device assignment, accept; every held epoch of a workgroup for a
// grant with history and for a principal that reads it by inheritance, ADR-058 section 2), then runs `checkBatch` on
// its own result. Wraps of epochs the batch does not create follow the transition rule `compatSkip`. It is a pure
// function of the log: after `seq_conflict` the caller re-reads the log and builds again.
import { formatTime } from '../encoding/time';
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import { Secret } from '../crypto/secret';
import { buildEntries, type Body, type Entry, type LogFields, type LogSigner } from './entries';
import { excludedFor, type PlatformState } from './platform';
import { epochKey, ORG, type OrgState, pairKey, requiredSet, type Scope, splitKey, WG, WRITE } from './state';
import { appendEntries, type VerifiedLog } from './verify';
import { buildWraps, compatSkip, type KeyRing } from './catchup';
import { checkWrap, compareSlots, slotKey, type WrapRecord, type WrapSlot } from './wraps';

export { requiredSet };

/** Entry types whose R is owed by the others (`OrgState.pending`) when the subject is the author itself. */
export const SELF_OWED: ReadonlySet<string> = new Set(['remove', 'role']);

/** The entry owes no rotation in its own batch: genesis, or a self-`remove`/self-`role` (`SELF_OWED`). */
const owesNothing = (seq: number, body: Body): boolean =>
  seq === 0 || (SELF_OWED.has(body.type as string) && entrySubject(body) === body.by);

/** The principal an entry is about: `subject`, else `device`, else its author `by`. */
export function entrySubject(body: Body): string {
  if (Object.hasOwn(body, 'subject')) return String(body.subject);
  if (Object.hasOwn(body, 'device')) return String(body.device);
  return String(body.by);
}

/** R in the vector/API form `[{scope, scope_id}]`, sorted. */
export function asJson(scopes: Iterable<string>): { scope: string; scope_id: string }[] {
  return [...scopes].sort().map((key) => {
    const [scope, scopeId] = splitKey(key);
    return { scope, scope_id: scopeId };
  });
}

/** The verified log after the batch, the rotate set R it had to contain and the wraps it stored. */
export interface BatchResult {
  readonly log: VerifiedLog;
  readonly required: ReadonlySet<string>;
  readonly wraps: readonly WrapSlot[];
}

function createdScopes(body: Body): Scope[] {
  switch (body.type) {
    case 'genesis':
      return [
        [ORG, body.org as string],
        [WG, body.default_wg as string],
      ];
    case 'wg_create':
      return [[WG, body.wg as string]];
    case 'rotate':
      return [[body.scope as string, body.scope_id as string]];
    default:
      return [];
  }
}

/** Options of `checkBatch`. */
export interface BatchOptions {
  /** Needed only for a new org (`log` null): the creator's registered signing key. */
  readonly founder?: string;
  readonly platform?: PlatformState | null;
  /** Hub receipt time (unix ms); null for a client-side check. */
  readonly now?: number | null;
  readonly platformAccess?: boolean;
  /** The decertified platform devices (`PlatformState.excluded`). */
  readonly excluded?: Iterable<string>;
}

/** Checks one batch; returns the new verified log. */
export async function checkBatch(
  log: VerifiedLog | null,
  entries: readonly unknown[],
  wraps: readonly unknown[],
  wrappedBy: string,
  options: BatchOptions = {},
): Promise<BatchResult> {
  const skip = [...(options.excluded ?? [])];
  const after = await appendEntries(log, entries, options.founder ?? '', {
    platform: options.platform ?? null,
    now: options.now ?? null,
    platformAccess: !!options.platformAccess,
  });
  const first = after.entries.length - entries.length;
  const bodies = after.entries.slice(first).map((entry) => entry.body);
  const required = new Set<string>();
  let lastTrigger = -1;
  const rotates: [number, string][] = [];
  for (const [index, body] of bodies.entries()) {
    const seq = first + index;
    if (body.type === 'rotate') rotates.push([index, pairKey(body.scope as string, body.scope_id as string)]);
    if (owesNothing(seq, body)) continue;
    const lost = requiredSet(after.states[seq - 1]!, after.states[seq]!, entrySubject(body));
    if (lost.size > 0) {
      for (const scope of lost) required.add(scope);
      lastTrigger = index;
    }
  }
  if (required.size > 0) {
    const scopes = new Set(rotates.map(([, scope]) => scope));
    const exact = rotates.length === required.size && scopes.size === required.size;
    if (!exact || [...scopes].some((s) => !required.has(s)) || rotates.some(([i]) => i < lastTrigger)) {
      fail('rotation_required');
    }
  }
  const stored: WrapSlot[] = [];
  for (const wrap of wraps) stored.push(await checkWrap(wrap, wrappedBy, after, skip, true));
  const slots = new Set(stored.map(slotKey));
  const state = after.state;
  const created = new Set(bodies.flatMap((body) => createdScopes(body).map((scope) => pairKey(...scope))));
  for (const key of created) {
    const [scope, scopeId] = splitKey(key);
    const epoch = state.epoch(scope, scopeId);
    const recipients = state.readers(scope, scopeId, skip);
    if (recipients.some((recipient) => !slots.has(slotKey({ scope, scope_id: scopeId, epoch, recipient })))) {
      fail('rotation_required');
    }
  }
  for (const body of bodies) {
    const subject = body.subject as string;
    if (body.type !== 'admit' || state.member(subject) === null) continue;
    const needed: Scope[] = [
      [ORG, state.org],
      [WG, state.defaultWg],
    ];
    const missing = needed.some(
      ([scope, scopeId]) =>
        !slots.has(slotKey({ scope, scope_id: scopeId, epoch: state.epoch(scope, scopeId), recipient: subject })),
    );
    if (missing) fail('log_admit');
  }
  return { log: after, required, wraps: stored };
}

// --- builder ---

/** The appending client: its principal ID and identity (`invite`: `sk_inv` of an `accept`). */
export interface BatchAuthor {
  readonly id: string;
  readonly identity: Identity;
  readonly invite?: CryptoKey;
}

/** A scope key created by the batch; the caller keeps it (it is wrapped for every reader, the author if it reads). */
export interface NewKey {
  readonly scope: string;
  readonly scope_id: string;
  readonly epoch: number;
  readonly key: Secret;
}

/** Input of `buildBatch`. */
export interface BatchInput {
  /** The verified log (null for a new org: the entries start with `genesis`). */
  readonly log: VerifiedLog | null;
  /** The org ID of a new org (only with `log` null). */
  readonly org?: string;
  readonly author: BatchAuthor;
  /** The requested entries in order (without the `rotate` entries R requires; those are added). */
  readonly entries: readonly LogFields[];
  /** The scope keys the author holds (to give newly entitled principals the current epochs). */
  readonly keys?: KeyRing;
  /** For the `grant` entries of the batch: also wrap the older epochs of that workgroup the author holds for its
   *  subject ("also give access to history", 06-web.md section 7.1). A principal that newly reads a workgroup by
   *  inheritance (`role` → admin) always gets them (ADR-058 section 2); other newly entitled principals get the
   *  current epochs only. */
  readonly history?: boolean;
  /** The verified platform state (`device_assign` with `platform_seq`, decertified devices). */
  readonly platform?: PlatformState | null;
  /** Events sent with the batch (e.g. `wg.profile`); passed through unchanged. */
  readonly events?: readonly unknown[];
  /** Header time of the entries (default: now). */
  readonly ts?: string;
  /** Source of fresh scope keys (default: 32 random bytes). */
  readonly newKey?: () => Secret;
}

/** The body of `POST /v1/orgs/{org}/log` (03-api.md section 4.3). */
export interface AppendBody {
  readonly expected_seq: number;
  readonly entries: Entry[];
  readonly wraps: WrapRecord[];
  readonly events: unknown[];
}

/** What `buildBatch` returns: the request body, the new keys, R and the log as it will be after the append. */
export interface BuiltBatch {
  readonly body: AppendBody;
  readonly newKeys: readonly NewKey[];
  readonly required: readonly { scope: string; scope_id: string }[];
  readonly log: VerifiedLog;
}

/**
 * Builds the batch for `input.entries`: R from the resulting states, `rotate` entries appended after the requested
 * ones, fresh keys for every epoch the batch creates, wraps of those keys for every remaining recipient and of the
 * held current (and with `history` older) epochs for newly entitled principals. The result passes `checkBatch`.
 */
export async function buildBatch(input: BatchInput): Promise<BuiltBatch> {
  const platform = input.platform ?? null;
  const founder = input.author.identity.public().sig;
  const options = { platform };
  const ts = input.ts ?? formatTime(Date.now());
  const org = input.log?.state.org ?? input.org ?? fail('log_genesis');
  // a new org is created by `genesis` alone (`POST /v1/orgs` carries one entry)
  if (input.log === null && input.entries.length !== 1) fail('log_genesis');
  const signer: LogSigner = {
    id: input.author.id,
    key: input.author.identity.sig.privateKey,
    ...(input.author.invite && { invite: input.author.invite }),
  };
  // 1. the requested entries, and R over them (a self-remove or self-demotion owes its rotation instead: `pending`)
  const requested = await buildEntries({ org, head: input.log?.head ?? null }, signer, input.entries, ts);
  const trial = await appendEntries(input.log, requested, founder, options);
  const first = trial.entries.length - requested.length;
  const required = new Set<string>();
  for (const [index, entry] of requested.entries()) {
    const seq = first + index;
    const body = entry.body;
    if (owesNothing(seq, body)) continue;
    for (const scope of requiredSet(trial.states[seq - 1]!, trial.states[seq]!, entrySubject(body)))
      required.add(scope);
  }
  // 2. one rotate per element of R, after the requested entries
  const rotations: LogFields[] = [...required].sort().map((key) => {
    const [scope, scopeId] = splitKey(key);
    return {
      type: 'rotate',
      scope: scope as 'org' | 'wg',
      scope_id: scopeId,
      epoch: trial.state.epoch(scope, scopeId) + 1,
    };
  });
  const rotateEntries = await buildEntries({ org, head: trial.head }, signer, rotations, ts);
  const entries = [...requested, ...rotateEntries];
  const after = rotateEntries.length === 0 ? trial : await appendEntries(trial, rotateEntries, founder, options);
  const state = after.state;
  const skip = excludedFor(platform, state);
  // 3. fresh keys for every created epoch (all are current at the end: a scope is created at most once per batch)
  const newKey = input.newKey ?? (() => Secret.random(32));
  const created = [
    ...new Set(entries.flatMap((entry) => createdScopes(entry.body).map((scope) => pairKey(...scope)))),
  ].sort();
  const newKeys: NewKey[] = [];
  for (const key of created) {
    const [scope, scopeId] = splitKey(key);
    newKeys.push({ scope, scope_id: scopeId, epoch: state.epoch(scope, scopeId), key: newKey() });
  }
  // 4. the slots: every reader of a new epoch, and the newly entitled principals of the other scopes (with a null
  //    log every scope is created by `genesis`, so `before` is only read for an existing org); wraps of epochs the
  //    batch does not create skip what the transition rule withholds (`compatSkip`)
  const before = input.log?.state ?? null;
  const history = new Set(
    input.history ? input.entries.flatMap((e) => (e.type === 'grant' ? [pairKey(e.subject, e.wg)] : [])) : [],
  );
  const slots: WrapSlot[] = [];
  for (const { scope, scope_id: scopeId, epoch } of newKeys) {
    for (const recipient of state.readers(scope, scopeId, skip))
      slots.push({ scope, scope_id: scopeId, epoch, recipient });
  }
  for (const [scope, scopeId] of state.scopes()) {
    if (created.includes(pairKey(scope, scopeId))) continue;
    const current = state.epoch(scope, scopeId);
    for (const recipient of state.readers(scope, scopeId, skip)) {
      if (before!.canRead(recipient, scope, scopeId)) continue;
      const older = scope === WG && (history.has(pairKey(recipient, scopeId)) || state.inherited(recipient, scopeId));
      for (let epoch = older ? 1 : current; epoch <= current; epoch += 1) {
        const slot = { scope, scope_id: scopeId, epoch, recipient };
        if (!compatSkip(state, input.author.id, slot)) slots.push(slot);
      }
    }
  }
  slots.sort(compareSlots);
  const fresh = new Map(newKeys.map((k) => [epochKey(k.scope, k.scope_id, k.epoch), k.key]));
  const held = input.keys ?? (() => null);
  const keys: KeyRing = (scope, scopeId, epoch) =>
    fresh.get(epochKey(scope, scopeId, epoch)) ?? held(scope, scopeId, epoch);
  const wraps = await buildWraps(input.author.identity, slots, keys, [state]);
  // 5. the batch must pass the hub's rules (client side: without receipt time)
  await checkBatch(input.log, entries, wraps, input.author.id, { founder, platform, excluded: skip });
  return {
    body: { expected_seq: first, entries, wraps, events: [...(input.events ?? [])] },
    newKeys,
    required: asJson(required),
    log: after,
  };
}

/**
 * The batch of the pending-rotation job (06-web.md section 11 job 4): one `rotate` for every scope of
 * `rotation_pending` the author may rotate (org: admin; wg: admin or writer), with wraps for all recipients. Null if
 * there is nothing the author may rotate.
 */
export function pendingRotation(
  log: VerifiedLog,
  input: Omit<BatchInput, 'log' | 'entries'>,
  pending: Iterable<Scope> = log.state.pendingScopes(),
): Promise<BuiltBatch> | null {
  const state = log.state;
  const me = input.author.id;
  const allowed = [...pending].filter(([scope, scopeId]) =>
    scope === ORG
      ? scopeId === state.org && state.isAdmin(me)
      : state.workgroups.has(scopeId) && (state.isAdmin(me) || state.wgAccess(me, scopeId) === WRITE),
  );
  if (allowed.length === 0) return null;
  const entries: LogFields[] = allowed.map(([scope, scopeId]) => ({
    type: 'rotate',
    scope: scope as 'org' | 'wg',
    scope_id: scopeId,
    epoch: state.epoch(scope, scopeId) + 1,
  }));
  return buildBatch({ ...input, log, entries });
}

/**
 * The entries that end every assignment of `device` (ADR-075 section 5): `device_unassign_org` when it is assigned to
 * the organization, then `device_unassign` for every explicit assignment (sorted by workgroup).
 */
export function unassignAll(state: OrgState, device: string): LogFields[] {
  const org: LogFields[] = state.orgAssigned(device) ? [{ type: 'device_unassign_org', device }] : [];
  const explicit = state.member(device) === null ? [] : [...state.assignments.keys()].map(splitKey);
  return [
    ...org,
    ...explicit
      .filter(([dev]) => dev === device)
      .map(([, wg]) => wg)
      .sort()
      .map((wg): LogFields => ({ type: 'device_unassign', device, wg })),
  ];
}

/**
 * The batch an admin owes after a platform device was decertified (04-crypto.md section 4.7): `device_unassign_org`
 * when it is assigned to the organization and `device_unassign` for every explicit assignment of the decertified
 * device (`unassignAll`), plus R (built by `buildBatch`). Computed from the verified platform log, so it does not
 * depend on the hub's `rotation_pending`. Null if nothing is owed.
 */
export function decertificationBatch(
  log: VerifiedLog,
  platform: PlatformState,
  input: Omit<BatchInput, 'log' | 'entries' | 'platform'>,
): Promise<BuiltBatch> | null {
  const state = log.state;
  const entries = platform.excluded(state).flatMap((device) => unassignAll(state, device));
  return entries.length === 0 ? null : buildBatch({ ...input, log, entries, platform });
}

/** The body of `POST /v1/orgs` (03-api.md section 4.3) from a genesis batch. */
export function createOrgBody(
  built: BuiltBatch,
  platformInviteId?: string,
): { genesis: Entry; wraps: WrapRecord[]; events: unknown[]; platform_invite_id?: string } {
  const body = { genesis: built.body.entries[0]!, wraps: built.body.wraps, events: built.body.events };
  return platformInviteId === undefined ? body : { ...body, platform_invite_id: platformInviteId };
}
