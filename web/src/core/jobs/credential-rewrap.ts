// Job 5 of 06-web.md section 11: credential re-wrap (04-crypto.md sections 5.10 and 6.3 W2, 03-api.md section 4.8).
//
// A device is entitled to the user's credential key `CK` while it is an active device assigned to a workgroup in
// which the user has access (`credReaders` over the verified logs of all the user's orgs, decertified platform devices
// excluded).
// - **A device becomes eligible:** its wrap of the current `CK` is created by the catch-up job (job 3, `credWraps`:
//   the hub offers the missing `cred` slot, the browser checks it against the verified logs and wraps). This job adds
//   nothing there, so a slot is never wrapped twice.
// - **A device loses eligibility:** it must not read credentials stored from now on, so `CK` is rotated: a fresh
//   `CK_{e+1}`, wrapped for the user and every device entitled now, and every record of the user re-encrypted under
//   it by the user (same ID and kind, `version + 1`), atomically in `POST /v1/me/credentials/rotate`. A toast tells
//   the user (slice `credentials`).
//
// When is a rotation owed? The browser decides from what it verified, plus the hub's list of the current holders:
// - `credentialLosses` replays the verified logs: after every entry, the devices that were readers of the user's `CK`
//   before and are not after it (an unassignment, a removal, an `ungrant` of the user, the user's own removal) and
//   that are not readers now (through any org) lost eligibility at the entry's signed `ts`; a decertified platform
//   device at the signed `ts` of its newest `device_decertify`;
// - `removalLosses`: an org whose verified log the browser holds but that `GET /v1/me` no longer lists (the user was
//   removed and cannot read the `remove` entry): its devices that are readers through no other org are lost now;
// - `holderLosses` (KOR-04, AUD-01 A-07): the hub names the devices holding a wrap of the user's current `CK`
//   (`cred_holders` of `GET /v1/me`, IDs only); one that no verified log of an org the user belongs to entitles is lost
//   now – this covers an org the user left while no browser of the user held its log. The hub can only cause a
//   rotation this way, never prevent one the logs owe (the new wraps go to the readers of the verified logs only). It
//   counts once the browser has a verdict on every org `GET /v1/me` lists (verified at least up to the hub's head, or
//   blocked: a blocked org entitles nobody); while one is still being verified it waits for the next run, so a fresh
//   sign-in or a log that has not caught up does not rotate the key away from an entitled device. Holders of another
//   epoch than the newest own `CK` are ignored (another browser's rotation is in flight). A holder loss is handled
//   once per device and sign-in, whatever the epoch: when another view (a browser that does not block an org this
//   one blocks, or that verified a newer log) wraps the next `CK` for the device again, this browser does not keep
//   rotating against it. Against a hub that leaves holders out the check can only be as good as the hub (trust
//   boundary: the hub may withhold); the log-based losses above stay independent of it. Records written by such a
//   holder are deleted like those of any device that lost eligibility (the browser cannot know its keys: it never
//   held that org's log), instead of blocking the rotation.
// A rotation is owed for a loss not yet handled in this sign-in that is not clearly older than the start of the
// current epoch (the hub's `created_at` of the user's own self-wrap of the epoch) – "clearly" allows `SKEW_MS` between
// the entry signer's clock and the hub's (signed requests are bounded to 120 s). An unknown or future epoch start
// fails closed (rotate). The in-session `handled` set prevents repeated rotations for the same loss; after a new
// sign-in a loss within the margin of the newest epoch can cause one more rotation, which only costs a re-encryption.
//
// Records: written by the user or a device entitled now → re-encrypted by the user. Written by a device that has lost
// eligibility → deleted (re-signing them as the user's would launder whatever that device or a colluding hub wrote;
// the user connects again). A rotation runs silently (ADR-058 section 4). Any other record the browser cannot open
// (signature, body, epoch) blocks the rotation – nothing is deleted on the browser's own doubt – and a notice asks the
// user to delete or replace it (shown once per sign-in).
//
// `CK` belongs to the user while jobs run per org queue: only the queue of the first verified org acts, runs are
// serialized per store (`exclusive`), and a run that finds the rotation done by another run or browser does nothing.
// A `409 version_conflict` (another browser rotated or wrote a record in between) is retried once from the start.
//
// GitHub identities (ADR-078 section 1): the rotation body holds the primary records only (`/v1/me/credentials` lists
// one record per kind, the contract allows four records); right after the rotation the user's further `github` records
// are re-sealed under the new epoch (`rewrapOwnIdentities`, same ID, version + 1; the identity module is imported only
// then, so it stays out of the first load, 06-web.md section 12.2). Should that fail, the job `github-rewrap` does it
// on its next run.
import { formatTime } from '../encoding/time';
import { Secret } from '../crypto/secret';
import {
  deleteCredential,
  fetchRecord,
  listCredentials,
  openRecord,
  type Opened,
  ownKeys,
  sealRecord,
  type CredentialKind,
} from '../credentials/records';
import { buildWraps } from '../log/catchup';
import { excludedFor, type VerifiedPlatformLog } from '../log/platform';
import { DEVICE, type OrgState } from '../log/state';
import type { VerifiedLog } from '../log/verify';
import { compareSlots, CRED, credReaders } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { deviceKeys, type RequestsContext } from '../requests/client';
import { noteCredentialRotation } from '../store/slices/credentials';
import type { OrgsState } from '../store/slices/orgs';
import type { Store } from '../store/store';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';

/** Allowed difference between the clock that signed a log entry and the hub's clock (module comment). */
export const SKEW_MS = 300_000;

/** A device that lost eligibility for the owner's `CK`: when, and the key that marks the loss as handled. */
export interface Loss {
  readonly at: number;
  readonly key: string;
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
    for (let seq = 1; seq < log.states.length; seq += 1) {
      const after = credReaders([log.states[seq]!], owner);
      for (const device of credReaders([log.states[seq - 1]!], owner)) {
        if (!after.has(device)) note(device, Date.parse(log.entries[seq]!.body.ts as string));
      }
    }
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

/** The verified logs of the user's orgs (blocked orgs left out), ordered by org ID. */
export function verifiedLogs(store: Store): VerifiedLog[] {
  return Object.values(store.getState().orgs.byId)
    .flatMap((entry) => (entry.log === null || entry.blocked !== null ? [] : [entry.log]))
    .sort((a, b) => Number(a.state.org > b.state.org) - Number(a.state.org < b.state.org));
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
  const keys = await ownKeys(context, userId, identity);
  const latest = keys[0];
  if (latest === undefined) return null;
  const lostHolders = me.holders?.epoch === latest.epoch ? fromHolders : new Map<string, Loss>();
  const losses = new Map([...fromLogs, ...lostHolders]);
  if (!rotationOwed(losses, latest.createdAt, context.now(), handled)) return null;
  const states = logs.filter((log) => member.has(log.state.org)).map((log) => log.state);
  const allStates = logs.map((log) => log.state);
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
      opened.push(await openRecord(context, item.kind, record, keys));
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
  const epoch = latest.epoch + 1;
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
    // the further GitHub identities follow at once (module comment); a failure is left to the job `github-rewrap`
    await import('../github/identities').then((identities) => identities.rewrapOwnIdentities(context));
  } catch {
    // the rotation itself is done
  }
  return { status: 'rotated', epoch, records: records.length, dropped: dropped.length };
}

/** Runs of one store, one at a time (`CK` belongs to the user, the queues are per org). */
const running = new WeakMap<Store, Promise<unknown>>();

export function exclusive<T>(store: Store, task: () => Promise<T>): Promise<T> {
  const before = running.get(store) ?? Promise.resolve();
  const next = before.then(task);
  running.set(
    store,
    next.catch(() => undefined),
  );
  return next;
}

/** Losses a rotation of this sign-in has handled (and blocked notices shown), per store and sign-in. */
const handledSets = new WeakMap<Store, { generation: number; keys: Set<string> }>();

export function handledFor(store: Store): Set<string> {
  const known = handledSets.get(store);
  if (known !== undefined && known.generation === store.generation) return known.keys;
  const fresh = { generation: store.generation, keys: new Set<string>() };
  handledSets.set(store, fresh);
  return fresh.keys;
}

/**
 * One run: only the queue of the first verified org acts (`CK` is per user, the trigger reaches every org's queue),
 * the rotation (retried once after a `version_conflict`), silent unless it is blocked (then a toast).
 */
export async function rewrap(context: JobContext, run: JobRun): Promise<RotationResult | null> {
  if (verifiedLogs(context.store)[0]?.state.org !== run.org) return null;
  const requests: RequestsContext = { store: context.store, client: context.client, now: context.now };
  const stillCurrent = () => current(context, run);
  const handled = handledFor(context.store);
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
    return result;
  });
}

export const job: JobRegistration = {
  name: 'credential-rewrap',
  order: 35,
  triggers: ['login', 'log', 'interval'],
  run: async (context: JobContext, run: JobRun) => {
    await rewrap(context, run);
  },
};
