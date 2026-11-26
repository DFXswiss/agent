// Catch-up wrapping (04-crypto.md section 5.11, `GET /v1/orgs/{org}/catch-up`, 06-web.md section 11 job 3).
// TypeScript twin of `agent_hub.log.catchup`, plus the client side: the hub's offer is never trusted, only the slots
// the verified log entitles and the user may create (W1/W2) are wrapped, for the recipient key of the verified log.
//
// Any key holder computes from the verified log the recipients that are entitled to the current epoch of a scope
// but have no wrap yet. Older epochs are wrapped only on request (section 5.10), with one exception: an admin that
// reads a workgroup by inheritance gets every epoch of it (`catchUpEpochs`, ADR-058 section 2). The active agent of a
// user (ADR-082 section 9.4) gets what its user gets, and in addition every slot its user has since its current
// membership (`agentSlots`: also older epochs delivered on request or after a promotion, but none current before the
// user's last `accept`/`admit`; for `cred` every CK epoch the owner has a wrap of). A person of the support roster
// (ADR-096) gets every epoch of every workgroup released to support (`supportSlots`), so a person that joins support
// later receives the existing releases from any online holder.
//
// Transition rule (ADR-058 section 2, `compatSkip`): until every runner verifies ADR-058, the browser does not wrap a
// `wg` key for a device when its own user reads that workgroup only by inheritance and did not create the epoch (an
// older runner would ignore such a wrap while the hub keeps the first wrap of a slot).
import { b64uDecode } from '../encoding/b64u';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import { signWrap, wrapKey } from '../crypto/wrap';
import { excludedFor, type PlatformState } from './platform';
import { DEVICE, epochKey, NO_ROSTER, type OrgState, type Roster, WG } from './state';
import type { VerifiedLog } from './verify';
import {
  compareSlots,
  CRED,
  credReaders,
  mayWrap,
  mayWrapCred,
  slotKey,
  type WrapRecord,
  type WrapSlot,
} from './wraps';

/** The scope keys a client holds: the key of `(scope, scope_id, epoch)` or null. */
export type KeyRing = (scope: string, scopeId: string, epoch: number) => Secret | null;

/**
 * The epochs catch-up gives an entitled recipient: the current one, and for an admin reading a workgroup by
 * inheritance every epoch from 1 (ADR-058 section 2).
 */
export function catchUpEpochs(state: OrgState, scope: string, scopeId: string, recipient: string): number[] {
  const current = state.epoch(scope, scopeId);
  const first = scope === WG && state.inherited(recipient, scopeId) ? 1 : current;
  return Array.from({ length: current - first + 1 }, (_, index) => first + index);
}

/**
 * The transition rule of ADR-058 section 2: true if `wrapper` must not wrap `slot`, i.e. a `wg` key for a device
 * while the wrapper reads that workgroup only by inheritance and did not create the epoch.
 */
export function compatSkip(state: OrgState, wrapper: string, slot: WrapSlot): boolean {
  return (
    slot.scope === WG &&
    state.member(slot.recipient)?.kind === DEVICE &&
    state.inherited(wrapper, slot.scope_id) &&
    state.epochAuthors.get(epochKey(WG, slot.scope_id, slot.epoch)) !== wrapper
  );
}

/** Every slot of a user implies the same slot for the user's active agent (ADR-082 section 9.4). */
export function agentSlots(log: VerifiedLog, existing: Iterable<WrapSlot>): WrapSlot[] {
  const state = log.state;
  return [...existing].flatMap((slot) => {
    const agent = state.activeAgent(slot.recipient);
    if (agent === null || slot.epoch < joinedEpoch(log, slot.recipient, slot.scope, slot.scope_id)) return [];
    return [{ ...slot, recipient: agent.id }];
  });
}

/** The epoch of a scope current when `user` (re)joined: its current membership starts there. */
function joinedEpoch(log: VerifiedLog, user: string, scope: string, scopeId: string): number {
  return log.states[log.state.principals.get(user)!.joinedSeq]!.epoch(scope, scopeId);
}

/** Unique slots, sorted. */
const uniqueSorted = (slots: Iterable<WrapSlot>): WrapSlot[] =>
  [...new Map([...slots].map((slot) => [slotKey(slot), slot])).values()].sort(compareSlots);

/** Every epoch of every workgroup released to support, for every person of the roster (ADR-096). */
export function supportSlots(state: OrgState, roster: Roster): WrapSlot[] {
  return [...state.support.keys()].flatMap((wg) =>
    [...roster.keys()].flatMap((recipient) =>
      Array.from({ length: state.epoch(WG, wg) }, (_, index) => ({
        scope: WG,
        scope_id: wg,
        epoch: index + 1,
        recipient,
      })),
    ),
  );
}

/**
 * Sorted `org`/`wg` slots (current epochs, `catchUpEpochs`, the slots of agents, `agentSlots`, and every epoch of a
 * released workgroup for the support persons of `roster`, `supportSlots`) without a wrap that `wrapper` may create.
 */
export function missingWraps(
  log: VerifiedLog,
  wrapper: string,
  existing: Iterable<WrapSlot>,
  excluded: Iterable<string> = [],
  roster: Roster = NO_ROSTER,
): WrapSlot[] {
  const known = [...existing];
  const have = new Set(known.map(slotKey));
  const skip = [...excluded];
  const state = log.state;
  const slots = state.scopes().flatMap(([scope, scopeId]) =>
    state.readers(scope, scopeId, skip).flatMap((recipient) =>
      catchUpEpochs(state, scope, scopeId, recipient).map((epoch) => ({
        scope,
        scope_id: scopeId,
        epoch,
        recipient,
      })),
    ),
  );
  return uniqueSorted([...slots, ...agentSlots(log, known), ...supportSlots(state, roster)]).filter(
    (slot) => !have.has(slotKey(slot)) && mayWrap(log, slot, wrapper, skip, roster),
  );
}

/**
 * Sorted `cred` slots of the owner's current CK epoch without a wrap that `wrapper` may create, and for the owner's
 * agents every CK epoch the owner has a wrap of (ADR-082 section 9.4).
 */
export function missingCredWraps(
  logs: readonly VerifiedLog[],
  owner: string,
  wrapper: string,
  currentEpoch: number,
  existing: Iterable<WrapSlot>,
  excluded: Iterable<string> = [],
): WrapSlot[] {
  const known = [...existing];
  const have = new Set(known.map(slotKey));
  const skip = [...excluded];
  const states = logs.map((log) => log.state);
  const readers = credReaders(states, owner, skip);
  const agents = new Set(states.flatMap((state) => state.activeAgent(owner)?.id ?? []).filter((id) => readers.has(id)));
  const owned = known.filter((slot) => slot.scope === CRED && slot.scope_id === owner && slot.recipient === owner);
  const older = owned.flatMap((slot) => [...agents].map((agent) => ({ ...slot, recipient: agent })));
  const current = [...readers].map((recipient) => ({ scope: CRED, scope_id: owner, epoch: currentEpoch, recipient }));
  return uniqueSorted([...current, ...older]).filter(
    (slot) => !have.has(slotKey(slot)) && mayWrapCred(states, slot, wrapper, currentEpoch, skip),
  );
}

const isSlot = (slot: unknown): slot is WrapSlot => {
  const s = slot as Record<string, unknown> | null;
  return (
    typeof s?.scope === 'string' &&
    typeof s.scope_id === 'string' &&
    typeof s.recipient === 'string' &&
    Number.isInteger(s.epoch)
  );
};

/**
 * An epoch the verified log confirms for the slot's recipient: one `catchUpEpochs` gives it, or for an agent one its
 * user gets, or one its user held by the log (`epochHolders`) since its current membership (the hub's `agentSlots`
 * come from the user's wraps, which the client cannot see).
 */
function confirmedEpoch(log: VerifiedLog, slot: WrapSlot, roster: Roster): boolean {
  const state = log.state;
  // a support person gets every epoch of a released workgroup (`supportSlots`; `mayWrap` checks the range)
  if (slot.scope === WG && state.supportReads(roster, slot.recipient, slot.scope_id)) return true;
  const own = (recipient: string) => catchUpEpochs(state, slot.scope, slot.scope_id, recipient).includes(slot.epoch);
  if (own(slot.recipient)) return true;
  const user = state.member(slot.recipient)?.parent ?? null;
  if (user === null) return false;
  const held = log.epochHolders(slot.scope, slot.scope_id, slot.epoch).has(user);
  return own(user) || (held && slot.epoch >= joinedEpoch(log, user, slot.scope, slot.scope_id));
}

/**
 * The slots of the hub's catch-up answer that the verified log confirms: well-formed, an epoch `confirmedEpoch` gives
 * the recipient (the current one; every epoch for an admin reading a workgroup by inheritance; for an agent the
 * epochs of its user; every epoch of a released workgroup for a person of `roster`, ADR-096), recipient entitled now, `wrapper` allowed (W1/W2) and not withheld by the transition rule
 * (`compatSkip`). Anything else is dropped (a malicious hub could ask for a wrap to an outsider or for history).
 * Sorted, without duplicates.
 */
export function confirmedSlots(
  log: VerifiedLog,
  wrapper: string,
  offered: Iterable<unknown>,
  excluded: Iterable<string> = [],
  roster: Roster = NO_ROSTER,
): WrapSlot[] {
  const skip = [...excluded];
  const state = log.state;
  const unique = new Map([...offered].filter(isSlot).map((slot) => [slotKey(slot), slot]));
  return [...unique.values()]
    .filter(
      (slot) =>
        confirmedEpoch(log, slot, roster) &&
        mayWrap(log, slot, wrapper, skip, roster) &&
        !compatSkip(state, wrapper, slot),
    )
    .sort(compareSlots);
}

/**
 * The X25519 key (b64u) of a principal from the verified states, else of a support person from the roster of the
 * verified platform log (ADR-096; null if neither knows it).
 */
export function recipientKem(
  states: readonly OrgState[],
  principal: string,
  roster: Roster = NO_ROSTER,
): string | null {
  for (const state of states) {
    const found = state.principals.get(principal);
    if (found !== undefined) return found.kem;
  }
  return roster.get(principal)?.kem ?? null;
}

/**
 * Seals and signs the wraps of `slots` with the keys of `keys`; slots whose key the wrapper does not hold or whose
 * recipient no state knows are skipped. The recipient key always comes from the verified log, never from the hub.
 */
export async function buildWraps(
  wrapper: Identity,
  slots: readonly WrapSlot[],
  keys: KeyRing,
  states: readonly OrgState[],
  roster: Roster = NO_ROSTER,
): Promise<WrapRecord[]> {
  const out: WrapRecord[] = [];
  for (const slot of slots) {
    const key = keys(slot.scope, slot.scope_id, slot.epoch);
    const kem = recipientKem(states, slot.recipient, roster);
    if (key === null || kem === null) continue;
    const wrap = await wrapKey(b64uDecode(kem), slot, key);
    const sig = await signWrap(wrapper.sig.privateKey, slot, wrap);
    out.push({
      scope: slot.scope,
      scope_id: slot.scope_id,
      epoch: slot.epoch,
      recipient_id: slot.recipient,
      wrap,
      sig,
    });
  }
  return out;
}

/**
 * Catch-up job: the wraps to `POST /v1/wraps` for the hub's offer, after checking it against the verified log;
 * `platform` is the verified platform state (required once the org has platform devices, `excludedFor`; its support
 * roster names the support persons and their keys, ADR-096).
 */
export async function catchUpWraps(
  log: VerifiedLog,
  wrapperId: string,
  wrapper: Identity,
  offered: Iterable<unknown>,
  keys: KeyRing,
  platform: PlatformState | null,
): Promise<WrapRecord[]> {
  const excluded = excludedFor(platform, log.state);
  const roster = platform?.supportRoster() ?? NO_ROSTER;
  const slots = confirmedSlots(log, wrapperId, offered, excluded, roster);
  return await buildWraps(wrapper, slots, keys, [log.state], roster);
}
