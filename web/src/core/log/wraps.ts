// Wrap rules W1-W3 (04-crypto.md sections 4.2 and 6.3). TypeScript twin of `agent_hub.log.wraps`.
//
// Entitlement (who may receive a scope key) is the current read right of the verified log: members of the org
// scope, users with access to a workgroup (explicit, or inherited as admin, ADR-058) and devices assigned to it, and
// for the `cred` scope of a user the owner plus devices assigned to any workgroup in which the owner has **explicit**
// access, in any org in which the owner joined by its own entry, not by `admit` (section 2, ADR-007, ADR-058 section
// 1: the inherited read of an admin does not spread credentials; ADR-060: an admission without consent does not
// either). Decertified platform devices are passed as `excluded`.
// - W1: a principal may wrap a scope epoch for itself if it holds the key (it is entitled now).
// - W2: a holder may wrap for any principal that is entitled now. A holder of `(scope, scope_id, e)` is the creator
//   of epoch e, a principal that could read the scope while e was current, or a principal entitled now.
// - W3: the hub stores a wrap only if W1/W2 hold for `wrapped_by` in its log state; recipients re-check and ignore
//   violating wraps (`wrap_unauthorized`).
// The epoch must exist: `1 <= e <= current`. The wrapper's signature (section 4.2) is checked first
// (`signature_invalid`) under its key from an org in which it is active (else, for older wraps, from any org that
// knows it); an unknown wrapper is `wrap_unauthorized`, and with `requireActive` (hub) also a removed one.
import { b64uDecode } from '../encoding/b64u';
import { fail } from '../crypto/errors';
import * as F from '../crypto/fields';
import { verifyWrap } from '../crypto/wrap';
import { refusalCode } from './entries';
import { compareText, DEVICE, ORG, type OrgState, type Principal, WG } from './state';
import type { VerifiedLog } from './verify';

export const CRED = 'cred';
export const WRAP_KEYS = ['scope', 'scope_id', 'epoch', 'recipient_id', 'wrap', 'sig'];

/** `(scope, scope_id, epoch, recipient)`: the key of a stored wrap (and the target of `wrapKey`). */
export interface WrapSlot {
  readonly scope: string;
  readonly scope_id: string;
  readonly epoch: number;
  readonly recipient: string;
}

/** A wrap as sent to and stored by the hub (`contracts/common.json` `wrap`). */
export interface WrapRecord {
  readonly scope: string;
  readonly scope_id: string;
  readonly epoch: number;
  readonly recipient_id: string;
  readonly wrap: string;
  readonly sig: string;
}

/** A comparable key of a slot. */
export const slotKey = (slot: WrapSlot): string => `${slot.scope} ${slot.scope_id} ${slot.epoch} ${slot.recipient}`;

/** Orders slots by scope, scope ID, epoch (numerically) and recipient. */
export function compareSlots(a: WrapSlot, b: WrapSlot): number {
  return (
    compareText(a.scope, b.scope) ||
    compareText(a.scope_id, b.scope_id) ||
    a.epoch - b.epoch ||
    compareText(a.recipient, b.recipient)
  );
}

/** The API form (`GET /v1/orgs/{org}/catch-up`). */
export function slotJson(slot: WrapSlot): { scope: string; scope_id: string; epoch: number; recipient_id: string } {
  return { scope: slot.scope, scope_id: slot.scope_id, epoch: slot.epoch, recipient_id: slot.recipient };
}

/** A slot from its API form. */
export function slotOf(value: { scope: string; scope_id: string; epoch: number; recipient_id: string }): WrapSlot {
  return { scope: value.scope, scope_id: value.scope_id, epoch: value.epoch, recipient: value.recipient_id };
}

/**
 * The owner and every active device assigned to a workgroup in which the owner has explicit access, over all orgs in
 * which the owner joined by its own entry (an org that admitted the owner without its consent gets no credential key,
 * ADR-060).
 */
export function credReaders(states: Iterable<OrgState>, owner: string, excluded: Iterable<string> = []): Set<string> {
  const skip = new Set(excluded);
  const found = new Set([owner]);
  for (const state of states) {
    if (state.member(owner)?.consented !== true) continue;
    for (const device of state.principals.values()) {
      const eligible = state.deviceWgs(device.id).some((wg) => state.explicitAccess(owner, wg) !== null);
      if (device.kind === DEVICE && !skip.has(device.id) && eligible) found.add(device.id);
    }
  }
  return found;
}

/** W1/W2 for an `org` or `wg` scope of this log. */
export function mayWrap(log: VerifiedLog, slot: WrapSlot, wrappedBy: string, excluded: Iterable<string> = []): boolean {
  const state = log.state;
  if (!(slot.scope === ORG || slot.scope === WG)) return false;
  if (!(slot.epoch >= 1 && slot.epoch <= state.epoch(slot.scope, slot.scope_id))) return false;
  const readers = state.readers(slot.scope, slot.scope_id, excluded);
  if (!readers.includes(slot.recipient)) return false;
  const holders = log.epochHolders(slot.scope, slot.scope_id, slot.epoch);
  return wrappedBy === slot.recipient || holders.has(wrappedBy) || readers.includes(wrappedBy);
}

/** W1/W2 for the `cred` scope of user `slot.scope_id` (its epochs live outside the membership logs). */
export function mayWrapCred(
  states: readonly OrgState[],
  slot: WrapSlot,
  wrappedBy: string,
  currentEpoch: number,
  excluded: Iterable<string> = [],
): boolean {
  const readers = credReaders(states, slot.scope_id, excluded);
  return (
    slot.scope === CRED &&
    slot.epoch >= 1 &&
    slot.epoch <= currentEpoch &&
    readers.has(slot.recipient) &&
    (wrappedBy === slot.recipient || readers.has(wrappedBy))
  );
}

/**
 * The wrapper's key: from an org in which it is active, else (readers re-checking an older wrap) from any org that
 * knows it; `requireActive` (the hub storing a new wrap) refuses removed wrappers.
 */
function signer(states: readonly OrgState[], principal: string, requireActive: boolean): string {
  let known: Principal[] = states.flatMap((state) => state.member(principal) ?? []);
  if (known.length === 0 && !requireActive) known = states.flatMap((state) => state.principals.get(principal) ?? []);
  if (known.length === 0) fail('wrap_unauthorized');
  return known[0]!.sig;
}

async function slotAndSignature(
  wrap: unknown,
  wrappedBy: string,
  states: readonly OrgState[],
  requireActive: boolean,
): Promise<WrapSlot> {
  const data = F.requireKeys(wrap, WRAP_KEYS);
  if (!Number.isInteger(data.epoch)) fail('header_invalid');
  const slot = {
    scope: data.scope as string,
    scope_id: data.scope_id as string,
    epoch: data.epoch as number,
    recipient: data.recipient_id as string,
  };
  const key = signer(states, wrappedBy, requireActive);
  await verifyWrap(b64uDecode(key), slot, data.wrap as string, data.sig);
  return slot;
}

/**
 * W3 for an `org`/`wg` wrap `{scope, scope_id, epoch, recipient_id, wrap, sig}` created by `wrappedBy`. The hub
 * passes `requireActive` (a removed principal stores no wraps); recipients re-checking stored wraps leave it false,
 * so a wrap stays valid after its wrapper was removed.
 */
export async function checkWrap(
  wrap: unknown,
  wrappedBy: string,
  log: VerifiedLog,
  excluded: Iterable<string> = [],
  requireActive = false,
): Promise<WrapSlot> {
  const skip = [...excluded];
  const slot = await slotAndSignature(wrap, wrappedBy, [log.state], requireActive);
  if (!mayWrap(log, slot, wrappedBy, skip)) fail('wrap_unauthorized');
  return slot;
}

/** W3 for a `cred` wrap; `logs` are the verified logs of the owner's orgs, `currentEpoch` the owner's CK epoch. */
export async function checkCredWrap(
  wrap: unknown,
  wrappedBy: string,
  logs: readonly VerifiedLog[],
  currentEpoch: number,
  excluded: Iterable<string> = [],
  requireActive = false,
): Promise<WrapSlot> {
  const skip = [...excluded];
  const states = logs.map((log) => log.state);
  const slot = await slotAndSignature(wrap, wrappedBy, states, requireActive);
  if (!mayWrapCred(states, slot, wrappedBy, currentEpoch, skip)) fail('wrap_unauthorized');
  return slot;
}

/** A fetched wrap (`GET /v1/wraps`, `GET /v1/device/wraps`) with the principal that created it. */
export interface FetchedWrap {
  readonly wrap: unknown;
  readonly wrappedBy: string;
}

/** The outcome of the wrap verification job: the accepted wraps and the refused ones with their codes. */
export interface WrapCheck {
  readonly accepted: { readonly slot: WrapSlot; readonly wrap: WrapRecord }[];
  readonly ignored: { readonly wrap: unknown; readonly code: string }[];
}

/**
 * Wrap verification job (06-web.md section 11, job 2): every fetched `org`/`wg` wrap is checked (signature, W1-W2);
 * violations are ignored and counted, never used.
 */
export async function verifyWraps(
  fetched: readonly FetchedWrap[],
  log: VerifiedLog,
  excluded: Iterable<string> = [],
): Promise<WrapCheck> {
  const skip = [...excluded];
  const result: { accepted: WrapCheck['accepted'][number][]; ignored: WrapCheck['ignored'][number][] } = {
    accepted: [],
    ignored: [],
  };
  for (const item of fetched) {
    try {
      const slot = await checkWrap(item.wrap, item.wrappedBy, log, skip);
      result.accepted.push({ slot, wrap: item.wrap as WrapRecord });
    } catch (error) {
      result.ignored.push({ wrap: item.wrap, code: refusalCode(error) });
    }
  }
  return result;
}
