// Catch-up wrapping (04-crypto.md section 5.11, `GET /v1/orgs/{org}/catch-up`, 06-web.md section 11 job 3).
// TypeScript twin of `agent_hub.log.catchup`, plus the client side: the hub's offer is never trusted, only the slots
// the verified log entitles and the user may create (W1/W2) are wrapped, for the recipient key of the verified log.
//
// Any key holder computes from the verified log the recipients that are entitled to the current epoch of a scope
// but have no wrap yet. Older epochs are wrapped only on request (section 5.10), with one exception: an admin that
// reads a workgroup by inheritance gets every epoch of it (`catchUpEpochs`, ADR-059 section 2).
//
// Transition rule (ADR-059 section 2, `compatSkip`): until every runner verifies ADR-059, the browser does not wrap a
// `wg` key for a device when its own user reads that workgroup only by inheritance and did not create the epoch (an
// older runner would ignore such a wrap while the hub keeps the first wrap of a slot).
import { b64uDecode } from '../encoding/b64u';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import { signWrap, wrapKey } from '../crypto/wrap';
import { excludedFor, type PlatformState } from './platform';
import { DEVICE, epochKey, type OrgState, WG } from './state';
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
 * inheritance every epoch from 1 (ADR-059 section 2).
 */
export function catchUpEpochs(state: OrgState, scope: string, scopeId: string, recipient: string): number[] {
  const current = state.epoch(scope, scopeId);
  const first = scope === WG && state.inherited(recipient, scopeId) ? 1 : current;
  return Array.from({ length: current - first + 1 }, (_, index) => first + index);
}

/**
 * The transition rule of ADR-059 section 2: true if `wrapper` must not wrap `slot`, i.e. a `wg` key for a device
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

/** Sorted `org`/`wg` slots (current epochs, `catchUpEpochs`) without a wrap that `wrapper` may create. */
export function missingWraps(
  log: VerifiedLog,
  wrapper: string,
  existing: Iterable<WrapSlot>,
  excluded: Iterable<string> = [],
): WrapSlot[] {
  const have = new Set([...existing].map(slotKey));
  const skip = [...excluded];
  const state = log.state;
  return state
    .scopes()
    .flatMap(([scope, scopeId]) =>
      state.readers(scope, scopeId, skip).flatMap((recipient) =>
        catchUpEpochs(state, scope, scopeId, recipient).map((epoch) => ({
          scope,
          scope_id: scopeId,
          epoch,
          recipient,
        })),
      ),
    )
    .filter((slot) => !have.has(slotKey(slot)) && mayWrap(log, slot, wrapper, skip))
    .sort(compareSlots);
}

/** Sorted `cred` slots of the owner's current CK epoch without a wrap that `wrapper` may create. */
export function missingCredWraps(
  logs: readonly VerifiedLog[],
  owner: string,
  wrapper: string,
  currentEpoch: number,
  existing: Iterable<WrapSlot>,
  excluded: Iterable<string> = [],
): WrapSlot[] {
  const have = new Set([...existing].map(slotKey));
  const skip = [...excluded];
  const states = logs.map((log) => log.state);
  return [...credReaders(states, owner, skip)]
    .map((recipient) => ({ scope: CRED, scope_id: owner, epoch: currentEpoch, recipient }))
    .filter((slot) => !have.has(slotKey(slot)) && mayWrapCred(states, slot, wrapper, currentEpoch, skip))
    .sort(compareSlots);
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
 * The slots of the hub's catch-up answer that the verified log confirms: well-formed, an epoch `catchUpEpochs` gives
 * the recipient (the current one; every epoch for an admin reading a workgroup by inheritance), recipient entitled
 * now, `wrapper` allowed (W1/W2) and not withheld by the transition rule (`compatSkip`). Anything else is dropped (a
 * malicious hub could ask for a wrap to an outsider or for history). Sorted, without duplicates.
 */
export function confirmedSlots(
  log: VerifiedLog,
  wrapper: string,
  offered: Iterable<unknown>,
  excluded: Iterable<string> = [],
): WrapSlot[] {
  const skip = [...excluded];
  const state = log.state;
  const unique = new Map([...offered].filter(isSlot).map((slot) => [slotKey(slot), slot]));
  return [...unique.values()]
    .filter(
      (slot) =>
        catchUpEpochs(state, slot.scope, slot.scope_id, slot.recipient).includes(slot.epoch) &&
        mayWrap(log, slot, wrapper, skip) &&
        !compatSkip(state, wrapper, slot),
    )
    .sort(compareSlots);
}

/** The X25519 key (b64u) of a principal from the verified states (null if no state knows it). */
export function recipientKem(states: readonly OrgState[], principal: string): string | null {
  for (const state of states) {
    const found = state.principals.get(principal);
    if (found !== undefined) return found.kem;
  }
  return null;
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
): Promise<WrapRecord[]> {
  const out: WrapRecord[] = [];
  for (const slot of slots) {
    const key = keys(slot.scope, slot.scope_id, slot.epoch);
    const kem = recipientKem(states, slot.recipient);
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
 * `platform` is the verified platform state (required once the org has platform devices, `excludedFor`).
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
  return await buildWraps(wrapper, confirmedSlots(log, wrapperId, offered, excluded), keys, [log.state]);
}
