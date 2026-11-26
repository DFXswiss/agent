// The release of a workgroup to DFX support, from the organization's side (ADR-096 sections 2 to 4; 06-web.md
// section 8.1, workgroup settings). Who may: `OrgState.canRelease` (an active admin or a user with effective `write`).
//
// - The active releases and the current support persons with their names: `loadOrgSupport` (`./people.ts`).
// - Release (and read ↔ write): ONE `POST /v1/orgs/{org}/log` batch with `support_grant {wg, access}`, the wraps of
//   every epoch of the workgroup this browser holds to every person of the support roster – their keys from the verified
//   platform log (`supportRoster`), never from the hub – and one `support.info` event (scope `wg`, header `wg` =
//   `scope_id`, `task`/`session`/`ref` empty, `lh` = the grant) sealed under the current workgroup key with the names
//   support needs: `{org_name, wg_name, members: {user_id: name}}`, the members limited to the users that read the
//   workgroup and whose name is known. A change of the access only (the release is active) sends no wraps again. The
//   whole batch passes `checkBatch` with the roster before it is sent; `seq_conflict` re-reads the log and builds again.
// - Revoke: `support_revoke {wg}`, the `rotate` of the workgroup and the wraps of the new epoch for every reader of the
//   org (built by `buildBatch`: R of `support_revoke` is the workgroup) – the common `submitBatch` flow.
import { b64uDecode } from '../encoding/b64u';
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import type { Identity } from '../crypto/keys';
import { signWrap, wrapKey } from '../crypto/wrap';
import type { KeyRing } from '../log/catchup';
import { excludedFor } from '../log/platform';
import { buildBatch, checkBatch } from '../log/rotation';
import { type OrgState, type Roster, USER, WG } from '../log/state';
import type { WrapRecord } from '../log/wraps';
import {
  install,
  keyRing,
  type OrgContext,
  refuse,
  requireHead,
  submitBatch,
  type Submitted,
  verifiedPlatform,
  withLogRetry,
} from '../org/batch';
import type { Access } from '../store/slices/support';
import type { Directory } from '../tasks/directory';
import { rosterOf } from './names';

export const INFO_KIND = 'support.info';
/** Limits of `body_support_info` (contracts/events/objects.json). */
export const NAME_MAX = 200;
export const MEMBER_NAME_MAX = 100;
export const MEMBERS_MAX = 1000;

/** Cuts a name to `max` code points (the schema's limit); null for an empty one. */
function cut(name: string | null | undefined, max: number): string | null {
  const text = [...(name ?? '').trim()].slice(0, max).join('');
  return text === '' ? null : text;
}

/** The body of `support.info` for `wg`: names from the verified directory, IDs where a name is not known. */
export function infoBody(
  state: OrgState,
  directory: Directory,
  wg: string,
): { org_name: string; wg_name: string; members: Record<string, string> } {
  const members: Record<string, string> = {};
  const readers = state.readers(WG, wg).filter((id) => state.member(id)?.kind === USER);
  for (const id of readers.slice(0, MEMBERS_MAX)) {
    const name = cut(directory.members.get(id), MEMBER_NAME_MAX);
    if (name !== null) members[id] = name;
  }
  return {
    org_name: cut(directory.orgName, NAME_MAX) ?? state.org,
    wg_name: cut(directory.workgroups.get(wg), NAME_MAX) ?? wg,
    members,
  };
}

/** The `support.info` event of `wg` at `lh`, sealed under the current workgroup key of `state`. */
export async function infoEvent(
  state: OrgState,
  keys: KeyRing,
  author: { readonly id: string; readonly identity: Identity },
  wg: string,
  body: unknown,
  lh: number,
  now: number,
): Promise<unknown> {
  const epoch = state.epoch(WG, wg);
  const key = keys(WG, wg, epoch) ?? refuse('epoch_stale');
  const header = newHeader({
    id: uuid7(now),
    org: state.org,
    scope: WG,
    scope_id: wg,
    epoch,
    author: author.id,
    kind: INFO_KIND,
    ts: formatTime(now),
    lh,
    wg,
  });
  return sealEvent(key, header, bodyJson(body), author.identity.sig.privateKey);
}

/** The wraps of every epoch of `wg` the browser holds to every person of the roster (keys from the platform log). */
export async function supportWraps(
  wrapper: Identity,
  state: OrgState,
  roster: Roster,
  wg: string,
  keys: KeyRing,
): Promise<WrapRecord[]> {
  const out: WrapRecord[] = [];
  for (let epoch = 1; epoch <= state.epoch(WG, wg); epoch += 1) {
    const key = keys(WG, wg, epoch);
    if (key === null) continue;
    for (const person of roster.values()) {
      const slot = { scope: WG, scope_id: wg, epoch, recipient: person.id };
      const wrap = await wrapKey(b64uDecode(person.kem), slot, key);
      const sig = await signWrap(wrapper.sig.privateKey, slot, wrap);
      out.push({ scope: WG, scope_id: wg, epoch, recipient_id: person.id, wrap, sig });
    }
  }
  return out;
}

/** The access of the active release of `wg`, else null. */
export function releaseOf(state: OrgState, wg: string): Access | null {
  return state.supportRelease(wg) as Access | null;
}

/**
 * Releases `wg` to support with `access`, or changes the access of the active release (module comment). Returns the
 * number of attempts; refuses `forbidden` without the right and `params_invalid` when the access is unchanged.
 */
export async function grantRelease(
  context: OrgContext,
  org: string,
  wg: string,
  access: Access,
  directory: Directory,
): Promise<number> {
  let attempts = 0;
  await withLogRetry(context, org, async (actor, number) => {
    attempts = number;
    const state = actor.log.state;
    if (!state.workgroups.has(wg) || !state.writable(wg) || !state.canRelease(actor.userId, wg)) refuse('forbidden');
    const before = releaseOf(state, wg);
    if (before === access) refuse('params_invalid');
    const platform = verifiedPlatform(context.store);
    const roster = rosterOf(platform);
    const held = keyRing(context.store, org);
    const now = context.now();
    const built = await buildBatch({
      log: actor.log,
      author: { id: actor.userId, identity: actor.identity },
      entries: [{ type: 'support_grant', wg, access }],
      keys: held,
      platform,
      ts: formatTime(now),
    });
    const after = built.log.state;
    const extra = before === null ? await supportWraps(actor.identity, after, roster, wg, held) : [];
    const wraps = [...built.body.wraps, ...extra];
    await checkBatch(actor.log, built.body.entries, wraps, actor.userId, {
      platform,
      excluded: excludedFor(platform, after),
      roster,
    });
    const author = { id: actor.userId, identity: actor.identity };
    const info = await infoEvent(after, held, author, wg, infoBody(after, directory, wg), built.log.head.seq, now);
    const answer = await context.client.request('POST', `/v1/orgs/${encodeURIComponent(org)}/log`, {
      body: { ...built.body, wraps, events: [info] },
    });
    requireHead(answer, built.log);
    install(context.store, org, actor.userId, built);
  });
  return attempts;
}

/** Revokes the release of `wg` (with the rotation of the workgroup); null once no release is active. */
export function revokeRelease(context: OrgContext, org: string, wg: string): Promise<Submitted> {
  return submitBatch(context, org, (log, me) => {
    const state = log.state;
    if (!state.canRelease(me, wg)) refuse('forbidden');
    return releaseOf(state, wg) === null ? null : { entries: [{ type: 'support_revoke', wg }] };
  });
}
