// The work of the job "credential-rewrap" (`jobs/credential-rewrap.ts`, whose comment describes it): losses, the
// rotation of `CK` and the run, imported when a run needs it so that it stays out of the first load (06-web.md section
// 12.2).
import type { Prefs } from '../account/prefs';
import { formatTime } from '../encoding/time';
import { Secret } from '../crypto/secret';
import { b64uDecode } from '../encoding/b64u';
import { authorKey } from '../github/connection';
import { rewrapOwnIdentities } from '../github/identities';
import {
  type AuthorKeys,
  deleteCredential,
  fetchRecord,
  listCredentials,
  openRecord,
  type Opened,
  listOwnKeys,
  sealRecord,
  type CredentialKind,
} from './records';
import { buildWraps } from '../log/catchup';
import { excludedFor, type VerifiedPlatformLog } from '../log/platform';
import { AGENT, changedPrincipals, DEVICE, type OrgState } from '../log/state';
import type { VerifiedLog } from '../log/verify';
import { compareSlots, CRED, credDevice, credReaders, revokedAgents } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { deviceKeys, type RequestsContext } from '../requests/client';
import { noteCredentialRotation } from '../store/slices/credentials';
import type { OrgsState } from '../store/slices/orgs';
import type { Store } from '../store/store';
import { current, type JobContext, type JobRun } from '../jobs/index';
import { exclusive, verifiedLogs } from '../jobs/credential-rewrap';

/** Allowed difference between the clock that signed a log entry and the hub's clock (module comment). */
export const SKEW_MS = 300_000;

/** A device that lost eligibility for the owner's `CK`: when, and the key that marks the loss as handled. */
export interface Loss {
  readonly at: number;
  readonly key: string;
}

/**
 * Whether the devices of `state` count for the owner's `CK` in `credReaders([state], owner)`: always for an
 * organization; for a user while it is a member that joined by its own entry not signed by a revoked agent.
 */
function devicesCount(state: OrgState, owner: string): boolean {
  if (state.org === owner) return true;
  const user = state.member(owner);
  if (user === null || !user.consented) return false;
  return user.joinAgent === null || !revokedAgents([state]).has(user.joinAgent);
}

/**
 * `[seq, principal]` for every principal of `credReaders([states[seq - 1]], owner)` that is not in
 * `credReaders([states[seq]], owner)`, in that order. The readers are kept from state to state: after an entry only
 * the principals whose rights may have changed are asked again (`changedPrincipals`, which names the agents of every
 * named user too). Everything is asked again when the owner itself or one of its agents is named (the owner's agents,
 * their revocation and whether the owner's devices count depend on them), or for an unknown change (KOR-35: asking
 * every principal after every entry made the first sign-in into a big org quadratic).
 */
function replayLosses(log: VerifiedLog, owner: string): [number, string][] {
  const lost: [number, string][] = [];
  let readers = credReaders([log.states[0]!], owner);
  let counting = devicesCount(log.states[0]!, owner);
  for (let seq = 1; seq < log.states.length; seq += 1) {
    const before = log.states[seq - 1]!;
    const state = log.states[seq]!;
    const changed = changedPrincipals(before, state);
    const ownerChanged = (p: string) =>
      p === owner || before.principals.get(p)?.parent === owner || state.principals.get(p)?.parent === owner;
    let gone: string[];
    if (changed === null || [...changed].some(ownerChanged)) {
      const after = credReaders([state], owner);
      gone = [...credReaders([before], owner)].filter((principal) => !after.has(principal));
      readers = after;
      counting = devicesCount(state, owner);
    } else {
      // only principals other than the owner and its agents changed: of those only devices can be readers, and an
      // entry names at most one device (its subject), so `gone` is in the order of `credReaders` as well
      const reader = (p: string) => counting && credDevice(state, owner, p);
      gone = [...changed].filter((p) => readers.has(p) && !reader(p));
      for (const p of changed) {
        if (reader(p)) readers.add(p);
        else readers.delete(p);
      }
    }
    for (const principal of gone) lost.push([seq, principal]);
  }
  return lost;
}

/** Devices that lost eligibility for the owner's `CK` in the verified logs and are not entitled now (module comment). */
export function credentialLosses(
  logs: readonly VerifiedLog[],
  platform: VerifiedPlatformLog | null,
  owner: string,
): Map<string, Loss> {
  const states = logs.map((log) => log.state);
  const excluded = states.flatMap((state) => excludedFor(platform?.state ?? null, state));
  const now = credReaders(states, owner, excluded);
  const losses = new Map<string, Loss>();
  const note = (device: string, at: number) => {
    if (!now.has(device) && at > (losses.get(device)?.at ?? -Infinity))
      losses.set(device, { at, key: `${device} ${at}` });
  };
  for (const log of logs) {
    for (const [seq, lost] of replayLosses(log, owner)) note(lost, Date.parse(log.entries[seq]!.body.ts as string));
  }
  const certifications = platform?.entries ?? [];
  for (const device of credReaders(states, owner)) {
    if (now.has(device)) continue;
    const decertified = certifications.filter(
      (entry) => entry.body.type === 'device_decertify' && entry.body.device === device,
    );
    const newest = decertified.at(-1);
    if (newest !== undefined) note(device, Date.parse(newest.body.ts as string));
  }
  return losses;
}

/**
 * Devices entitled only through orgs the user no longer belongs to (`member` is the hub's org list of `GET /v1/me`):
 * the browser may not see the `remove` entry, so they count as lost now; handled once per such org and device.
 */
export function removalLosses(
  logs: readonly VerifiedLog[],
  member: ReadonlySet<string>,
  owner: string,
  now: number,
): Map<string, Loss> {
  const kept = logs.filter((log) => member.has(log.state.org)).map((log) => log.state);
  const remaining = credReaders(kept, owner);
  const losses = new Map<string, Loss>();
  for (const log of logs) {
    if (member.has(log.state.org)) continue;
    for (const device of credReaders([log.state], owner)) {
      if (device !== owner && !remaining.has(device))
        losses.set(device, { at: now, key: `${device} left ${log.state.org}` });
    }
  }
  return losses;
}

/** The current `CK` epoch and the devices holding a wrap of it, as `GET /v1/me` names them (`cred_holders`). */
export interface Holders {
  readonly epoch: number;
  readonly devices: readonly string[];
}

/** An org of `GET /v1/me`: its ID and, when given, the sequence number of the hub's log head. */
export interface MemberOrg {
  readonly org_id: string;
  readonly log_head?: { readonly seq?: unknown };
}

/**
 * Holders of the current `CK` that no verified log of an org the user belongs to entitles (module comment); empty
 * while an org of `member` has no verdict yet (not verified, or verified behind the hub's head, and not blocked).
 */
export function holderLosses(
  orgs: OrgsState,
  member: readonly MemberOrg[],
  holders: Holders | null,
  owner: string,
  now: number,
): Map<string, Loss> {
  const losses = new Map<string, Loss>();
  if (holders === null) return losses;
  const { byId, platform } = orgs;
  const states: OrgState[] = [];
  for (const org of member) {
    const entry = byId[org.org_id];
    if (entry?.blocked) continue;
    const head = org.log_head?.seq;
    if (!entry?.log || (typeof head === 'number' && head > entry.log.head.seq)) return losses;
    states.push(entry.log.state);
  }
  const excluded = states.flatMap((state) => excludedFor(platform?.state ?? null, state));
  const readers = credReaders(states, owner, excluded);
  for (const device of holders.devices) {
    if (device !== owner && !readers.has(device)) losses.set(device, { at: now, key: `${device} holds` });
  }
  return losses;
}

/**
 * Whether a rotation is owed: a loss not yet handled in this session that is not clearly older than the start of the
 * current epoch (`SKEW_MS` margin). An unknown epoch start, or one in the future, fails closed.
 */
export function rotationOwed(
  losses: ReadonlyMap<string, Loss>,
  epochStart: number | null,
  now: number,
  handled: ReadonlySet<string> = new Set(),
): boolean {
  const pending = [...losses.values()].filter((loss) => !handled.has(loss.key));
  if (pending.length === 0) return false;
  if (epochStart === null || epochStart > now + SKEW_MS) return true;
  return pending.some((loss) => loss.at >= epochStart - SKEW_MS);
}

/**
 * The signing key of an agent of `owner` that some verified state knows (active, replaced or revoked: the rotation
 * re-signs what an earlier agent of the user wrote, ADR-082 section 9.5), else null.
 */
export function ownAgentKey(states: readonly OrgState[], owner: string, author: unknown): Uint8Array | null {
  for (const state of states) {
    const found = state.principals.get(author as string);
    if (found?.kind === AGENT && found.parent === owner) return b64uDecode(found.sig);
  }
  return null;
}

/** Whether `author` is a device some verified state knows (it may have lost eligibility since). */
function knownDevice(states: readonly OrgState[], author: unknown): boolean {
  return states.some((state) => state.principals.get(author as string)?.kind === DEVICE);
}

export type RotationResult =
  | {
      readonly status: 'rotated';
      readonly epoch: number;
      readonly records: number;
      /** Records written by a device that lost eligibility, deleted instead of re-signed. */
      readonly dropped: number;
    }
  | {
      readonly status: 'blocked';
      /** Records the browser cannot open: nothing is rotated until the user deletes or replaces them. */
      readonly kinds: readonly CredentialKind[];
    };

/** The orgs and the current `CK` holders of `GET /v1/me` (anything malformed counts as absent). */
async function meOf(context: RequestsContext): Promise<{ orgs: MemberOrg[]; holders: Holders | null }> {
  const me = await context.client.request('GET', '/v1/me');
  const listed = isRecord(me) && Array.isArray(me.orgs) ? me.orgs.filter(isRecord) : [];
  const orgs = listed.filter((org): org is Record<string, unknown> & MemberOrg => typeof org.org_id === 'string');
  const found = isRecord(me) ? me.cred_holders : undefined;
  const holders =
    isRecord(found) &&
    Number.isSafeInteger(found.epoch) &&
    Array.isArray(found.devices) &&
    found.devices.every((device) => typeof device === 'string')
      ? { epoch: found.epoch as number, devices: found.devices as string[] }
      : null;
  return { orgs, holders };
}

/** One check and, when owed, one rotation (module comment); null when nothing was owed. */
export async function rotateCredentialKey(
  context: RequestsContext,
  handled: Set<string> = new Set(),
  stillCurrent: () => boolean = () => true,
): Promise<RotationResult | null> {
  const { store } = context;
  const { userId, identity } = store.getState().me;
  if (userId === null || identity === null) return null;
  // one snapshot of the verified logs for the check and the rotation (an org verified meanwhile waits for the next run)
  const orgs = store.getState().orgs;
  const logs = verifiedLogs(store);
  if (logs.length === 0) return null;
  const platform = orgs.platform;
  const me = await meOf(context);
  const member = new Set(me.orgs.map((org) => org.org_id));
  const fromLogs = new Map([
    ...credentialLosses(logs, platform, userId),
    ...removalLosses(logs, member, userId, context.now()),
  ]);
  const fromHolders = holderLosses(orgs, me.orgs, me.holders, userId, context.now());
  if (fromLogs.size === 0 && fromHolders.size === 0) return null;
  const { keys, opening, listed } = await listOwnKeys(context, userId, identity);
  // the epoch start comes from an epoch the browser builds on; one only a former agent wrapped counts as unknown
  const latest = keys[0] ?? opening[0];
  if (latest === undefined) return null;
  const lostHolders = me.holders?.epoch === latest.epoch ? fromHolders : new Map<string, Loss>();
  const losses = new Map([...fromLogs, ...lostHolders]);
  if (!rotationOwed(losses, keys[0]?.createdAt ?? null, context.now(), handled)) return null;
  const states = logs.filter((log) => member.has(log.state.org)).map((log) => log.state);
  const allStates = logs.map((log) => log.state);
  const authors: AuthorKeys = (author) =>
    authorKey(store, userId, identity, author) ?? ownAgentKey(allStates, userId, author);
  const opened: Opened[] = [];
  const dropped: CredentialKind[] = [];
  const blocked: CredentialKind[] = [];
  for (const item of await listCredentials(context)) {
    const record = await fetchRecord(context, item.kind);
    if (record === null) continue;
    const header = isRecord(record) && isRecord(record.header) ? record.header : {};
    const author = header.author;
    const lost = knownDevice(allStates, author) || lostHolders.has(author as string);
    if (author !== userId && deviceKeys(store, author as string) === null && lost) {
      dropped.push(item.kind);
      continue;
    }
    try {
      // records under an epoch only a former agent wrapped are opened, then re-encrypted under the new epoch
      opened.push(await openRecord(context, item.kind, record, opening, authors));
    } catch {
      blocked.push(item.kind);
    }
  }
  if (blocked.length > 0) {
    const key = `blocked ${blocked.join(' ')}`;
    if (handled.has(key)) return null;
    handled.add(key);
    return { status: 'blocked', kinds: blocked };
  }
  if (!stillCurrent()) return null;
  for (const kind of dropped) await deleteCredential(context, kind);
  // the newest listed self-wrap may be of an epoch this browser does not take: one a since revoked agent of the user
  // created (ADR-082 section 9.5); the new epoch follows it
  const epoch = Math.max(latest.epoch, listed) + 1;
  const key = Secret.random(32);
  const excluded = states.flatMap((state) => excludedFor(platform?.state ?? null, state));
  const slots = [...credReaders(states, userId, excluded)]
    .map((recipient) => ({ scope: CRED, scope_id: userId, epoch, recipient }))
    .sort(compareSlots);
  const wraps = await buildWraps(identity, slots, () => key, allStates);
  const ts = formatTime(context.now());
  const records = [];
  for (const item of opened) {
    records.push(
      await sealRecord({
        id: item.header.id as string,
        owner: userId,
        kind: item.summary.kind,
        version: (item.header.version as number) + 1,
        epoch,
        author: userId,
        ts,
        key,
        body: item.body,
        signer: identity.sig.privateKey,
      }),
    );
  }
  if (!stillCurrent()) return null;
  await context.client.request('POST', '/v1/me/credentials/rotate', { body: { epoch, wraps, records } });
  for (const loss of losses.values()) handled.add(loss.key);
  try {
    // the further GitHub identities follow at once (module comment); a failure is left to the job `github-rewrap`.
    // Imported statically: this module and the identities share the lazy chunk, and an import inside one chunk has no
    // chunk whose integrity it could check (`src/core/assets/integrity.ts`).
    await rewrapOwnIdentities(context);
  } catch {
    // the rotation itself is done
  }
  return { status: 'rotated', epoch, records: records.length, dropped: dropped.length };
}

/** Losses a rotation of this sign-in has handled (and blocked notices shown), per store and sign-in. */
const handledSets = new WeakMap<Store, { generation: number; keys: Set<string> }>();

/** At most this many handled losses are remembered in the browser's preferences (the newest). */
export const KEPT_LOSSES = 500;

/** Handled keys that stay in the browser: losses with a time and losses through a left org (module comment). */
const lasting = (key: string): boolean => !key.startsWith('blocked ') && !key.endsWith(' holds');

/**
 * The losses handled in this sign-in, started with those this browser remembers (`prefs`): a loss within the clock
 * margin of the epoch its own rotation created is not rotated for again at the next sign-in or reload.
 */
export function handledFor(store: Store, prefs?: Pick<Prefs, 'handledLosses'>): Set<string> {
  const known = handledSets.get(store);
  if (known !== undefined && known.generation === store.generation) return known.keys;
  const userId = store.getState().me.userId;
  const remembered = userId === null ? [] : (prefs?.handledLosses(userId) ?? []);
  const fresh = { generation: store.generation, keys: new Set<string>(remembered) };
  handledSets.set(store, fresh);
  return fresh.keys;
}

/** Remembers the lasting handled losses of `handled` in the browser's preferences (module comment). */
export function rememberHandled(
  store: Store,
  handled: ReadonlySet<string>,
  prefs?: Pick<Prefs, 'setHandledLosses'>,
): void {
  const userId = store.getState().me.userId;
  if (userId === null || prefs === undefined) return;
  prefs.setHandledLosses(userId, [...handled].filter(lasting).slice(-KEPT_LOSSES));
}

/**
 * One run: only the queue of the first verified org acts (`CK` is per user, the trigger reaches every org's queue),
 * the rotation (retried once after a `version_conflict`), silent unless it is blocked (then a toast).
 */
export async function rewrap(context: JobContext, run: JobRun): Promise<RotationResult | null> {
  if (verifiedLogs(context.store)[0]?.state.org !== run.org) return null;
  const requests: RequestsContext = { store: context.store, client: context.client, now: context.now };
  const stillCurrent = () => current(context, run);
  const handled = handledFor(context.store, context.prefs);
  return exclusive(context.store, async () => {
    let result: RotationResult | null;
    try {
      result = await rotateCredentialKey(requests, handled, stillCurrent);
    } catch (error) {
      if (!(error instanceof HubError) || error.code !== 'version_conflict') throw error;
      result = await rotateCredentialKey(requests, handled, stillCurrent);
    }
    if (result?.status === 'blocked' && stillCurrent()) {
      noteCredentialRotation(context.store, { result, at: context.now() });
    }
    if (result?.status === 'rotated') rememberHandled(context.store, handled, context.prefs);
    return result;
  });
}
