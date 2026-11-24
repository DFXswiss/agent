// Joining an org with an invitation code (04-crypto.md sections 5.1 steps 1-2, 5.2, 5.2a and 6.1; 06-web.md section
// 3.1 `org_verify`, `existing_account`, `accept`). Everything the invitee trusts comes from the code: the package opens
// only with the code's `package_key` (`invite_code_invalid`), it names the org, its founder key and the log head right
// after the `invite` entry; the membership log of the lookup answer (the invitee is not a member and cannot read the
// org's log) is verified from `seq` 0 against that founder (L1-L8), must contain the package head (`log_rollback`),
// must end at the served head (`log_chain`) and must hold this `invite` entry with the code's `pk_inv` – appended by an
// admin, which L5 checks – still open (`invite_not_in_log`, `invite_revoked`, `invite_consumed`). A log with platform
// devices (`device_assign` with `platform_seq`) needs the verified platform log, which only a signed-in user can read:
// before the sign-in such a log is anchored without signatures (gapless hash chain to the served head through the
// package head, genesis of the package's founder and org, the `invite` entry at or before the package head), and it
// is verified completely after the sign-in, before the `accept` (`completeInvitation`).
//
// `accept{invite_id, subject, kind:"user", pub}` carries `sig` (the joining identity) and `inv_sig` (`sk_inv`); the
// batch also holds the self-wraps of the current package keys (W1) and the `member.profile` event with the display
// name, sealed with the package's org key at the head of the `accept`. A concurrent append (`seq_conflict`) re-reads
// the log through a new lookup (it must extend the verified one) and rebuilds; an `accept` the hub already stored
// (answer lost) is recognized by the membership in `GET /v1/me`.
import { b64uDecode } from '../encoding/b64u';
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { fail } from '../crypto/errors';
import { newHeader, sealEvent } from '../crypto/event';
import { inviteSecrets, openPackage, parseCode } from '../crypto/invite';
import type { Identity } from '../crypto/keys';
import { Secret } from '../crypto/secret';
import { checkExtends, checkLogEntry, entryHash, type Head } from '../log/entries';
import { type PlatformState, verifyPlatformLog } from '../log/platform';
import { buildBatch } from '../log/rotation';
import { compareText, epochKey, ORG, READ, WG, WRITE } from '../log/state';
import { verifyLog, type VerifiedLog } from '../log/verify';
import { HubError } from '../net/signedFetch';
import { requireHead } from '../org/batch';
import { isRecord } from '../registry';
import type { AnonRequest } from './anon';
import { displayName } from './genesis';
import type { Lookup } from './register';

/** A scope key of the package. */
export interface PackageKey {
  readonly scope: string;
  readonly scope_id: string;
  readonly epoch: number;
  readonly key: Secret;
}

/** A workgroup the invitation gives access to (the default workgroup always, with write). */
export interface InvitedWorkgroup {
  readonly id: string;
  /** From the package (`wg_names`); empty if the inviter did not know it. */
  readonly name: string;
  readonly access: typeof READ | typeof WRITE;
  readonly isDefault: boolean;
}

/** An opened org invitation. */
export interface OrgInvitation {
  readonly inviteId: string;
  readonly org: string;
  readonly orgName: string;
  /** The inviter's personal message from the package (rule E-28), or null; shown as plain text only. */
  readonly message: string | null;
  readonly defaultWg: string;
  readonly role: string;
  readonly workgroups: readonly InvitedWorkgroup[];
  /** `expires` of the `invite` entry. */
  readonly expires: string;
  readonly founder: string;
  /** The log head right after the `invite` entry (from the package). */
  readonly packageHead: Head;
  readonly keys: readonly PackageKey[];
  /** `sk_inv`, signs `inv_sig`. */
  readonly signing: CryptoKey;
  /** The log of the last lookup and its served head. */
  readonly entries: readonly unknown[];
  readonly head: Head | null;
  /** The verified log; null while it waits for the platform log (`needsPlatform`). */
  readonly log: VerifiedLog | null;
}

const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const isHead = (value: unknown): value is Head => isRecord(value) && isSeq(value.seq) && typeof value.hash === 'string';

/** Whether verifying `entries` needs the platform log (an assignment of a platform device, or an `admit` whose
 * identities are anchored there, ADR-060). */
export function needsPlatform(entries: readonly unknown[]): boolean {
  return entries.some(
    (entry) =>
      isRecord(entry) &&
      isRecord(entry.body) &&
      ((entry.body.type === 'device_assign' && Object.hasOwn(entry.body, 'platform_seq')) ||
        entry.body.type === 'admit'),
  );
}

/** The log part of a lookup answer. */
function logOf(raw: Readonly<Record<string, unknown>>): { entries: unknown[]; head: Head | null } {
  if (!Array.isArray(raw.log) || raw.log.length === 0) fail('log_empty');
  return { entries: raw.log, head: isHead(raw.log_head) ? { seq: raw.log_head.seq, hash: raw.log_head.hash } : null };
}

/**
 * The `invite` entry of `inviteId` without the platform log (anonymous invitee, a log with platform devices): the
 * entries form a gapless hash chain (L1, L2) that ends at the served head and contains the package head, and genesis
 * names the package's founder and org. The package head pins the hash of every entry up to it – the history the
 * inviting admin verified – so the `invite` entry (at or before the package head) is authentic without the
 * signatures; entries after it are checked with the platform log after the sign-in (`completeInvitation`).
 */
async function anchoredInvite(
  invitation: Pick<OrgInvitation, 'org' | 'founder' | 'packageHead' | 'inviteId' | 'entries' | 'head'>,
): Promise<Record<string, unknown>> {
  const { entries, packageHead } = invitation;
  await checkExtends(packageHead, entries);
  let found: Record<string, unknown> | null = null;
  let previous = '';
  for (const [seq, entry] of entries.entries()) {
    const body = checkLogEntry(entry);
    if (body.seq !== seq || body.prev !== previous) fail('log_chain');
    previous = await entryHash(entry);
    if (seq <= packageHead.seq && body.type === 'invite' && body.invite_id === invitation.inviteId) found = body;
  }
  const genesis = checkLogEntry(entries[0]);
  const pub = genesis.pub as { sig: string } | undefined;
  if (genesis.type !== 'genesis' || pub!.sig !== invitation.founder || genesis.org !== invitation.org) {
    fail('log_genesis');
  }
  const served = invitation.head;
  if (served === null || served.seq !== entries.length - 1 || served.hash !== previous) fail('log_chain');
  return found ?? fail('invite_not_in_log');
}

interface InviteFields {
  readonly kind: string;
  readonly pkInv: string;
  readonly role: string | null;
  readonly wgs: Readonly<Record<string, string>>;
  readonly expires: string;
}

function fieldsOf(body: Record<string, unknown>): InviteFields {
  return {
    kind: body.kind as string,
    pkInv: body.pk_inv as string,
    role: body.role as string,
    wgs: body.wgs as Record<string, string>,
    expires: body.expires as string,
  };
}

/** The checks of the `invite` entry itself: this code's key, a user invitation, still open. */
function checkInvite(found: InviteFields & { revokedSeq?: number | null; acceptedSeq?: number | null }, pkInv: string) {
  if (found.pkInv !== pkInv) fail('invite_not_in_log');
  if (found.kind !== 'user') fail('invite_not_in_log');
  if (found.revokedSeq != null) fail('invite_revoked');
  if (found.acceptedSeq != null) fail('invite_consumed');
}

/**
 * Verifies the lookup log for the invitation (section 6.1 against the package's founder and head, the served head,
 * the `invite` entry). Returns the verified log.
 */
export async function verifyInvitationLog(
  invitation: Pick<OrgInvitation, 'org' | 'founder' | 'packageHead' | 'inviteId' | 'entries' | 'head'>,
  pkInv: string,
  platform: PlatformState | null,
): Promise<VerifiedLog> {
  await checkExtends(invitation.packageHead, invitation.entries);
  const log = await verifyLog(invitation.entries, invitation.founder, platform);
  if (log.state.org !== invitation.org) fail('log_genesis');
  const served = invitation.head;
  if (served === null || served.seq !== log.head.seq || served.hash !== log.head.hash) fail('log_chain');
  const found = log.state.invites.get(invitation.inviteId) ?? fail('invite_not_in_log');
  checkInvite(found, pkInv);
  return log;
}

function workgroupsOf(
  defaultWg: string,
  wgs: Readonly<Record<string, string>>,
  names: Readonly<Record<string, string>>,
): InvitedWorkgroup[] {
  const access = new Map<string, typeof READ | typeof WRITE>(
    Object.entries(wgs).map(([wg, value]) => [wg, value === WRITE ? WRITE : READ]),
  );
  access.set(defaultWg, WRITE);
  return [...access]
    .map(([id, value]) => ({ id, name: names[id] ?? '', access: value, isDefault: id === defaultWg }))
    .sort(
      (a, b) => Number(b.isDefault) - Number(a.isDefault) || a.name.localeCompare(b.name) || compareText(a.id, b.id),
    );
}

/**
 * `org_verify`: opens the package with the code and verifies the lookup log (`platform`: the verified platform state
 * if the user is signed in; without it a log with platform devices stays unverified until `completeInvitation`).
 */
export async function openInvitation(
  code: string,
  lookup: Lookup,
  platform: PlatformState | null,
): Promise<OrgInvitation> {
  const secrets = await inviteSecrets(parseCode(code));
  if (secrets.inviteId !== lookup.inviteId || typeof lookup.raw.package_ct !== 'string') fail('invite_code_invalid');
  const pkg = await openPackage(secrets, lookup.raw.package_ct);
  const org = pkg.org as string;
  const head = pkg.head as Head;
  const { entries, head: served } = logOf(lookup.raw);
  const base = {
    inviteId: secrets.inviteId,
    org,
    founder: pkg.founder as string,
    packageHead: { seq: head.seq, hash: head.hash },
    entries,
    head: served,
  };
  const deferred = platform === null && needsPlatform(entries);
  const log = deferred ? null : await verifyInvitationLog(base, secrets.pkInv, platform);
  const found = log === null ? fieldsOf(await anchoredInvite(base)) : log.state.invites.get(base.inviteId)!;
  if (log === null) checkInvite(found, secrets.pkInv);
  const defaultWg = log === null ? (checkLogEntry(entries[0]).default_wg as string) : log.state.defaultWg;
  const keys = (pkg.keys as { scope: string; scope_id: string; epoch: number; key: string }[]).map((item) => ({
    scope: item.scope,
    scope_id: item.scope_id,
    epoch: item.epoch,
    key: Secret.of(b64uDecode(item.key)),
  }));
  return {
    ...base,
    orgName: pkg.org_name as string,
    message: typeof pkg.message === 'string' ? pkg.message : null,
    defaultWg,
    role: found.role!,
    workgroups: workgroupsOf(defaultWg, found.wgs, pkg.wg_names as Record<string, string>),
    expires: found.expires,
    keys,
    signing: secrets.signing.privateKey,
    log,
  };
}

/** Verifies a deferred log with the platform state (no-op for a verified one). */
export async function completeInvitation(
  invitation: OrgInvitation,
  code: string,
  platform: PlatformState | null,
): Promise<OrgInvitation> {
  if (invitation.log !== null) return invitation;
  const secrets = await inviteSecrets(parseCode(code));
  const log = await verifyInvitationLog(invitation, secrets.pkInv, platform);
  return { ...invitation, log };
}

/**
 * A new lookup after `seq_conflict`: the log must extend the verified one (`log_rollback`) and is verified again.
 * An invitation the hub no longer knows rejects with `invite_unknown`.
 */
export async function refreshInvitation(
  anon: AnonRequest,
  invitation: OrgInvitation,
  code: string,
  platform: PlatformState | null,
): Promise<OrgInvitation> {
  const raw = await anon('POST', '/v1/invites/lookup', { invite_id: invitation.inviteId });
  if (!isRecord(raw)) fail('header_invalid');
  const { entries, head } = logOf(raw);
  if (invitation.log !== null) await checkExtends(invitation.log.head, entries);
  return completeInvitation({ ...invitation, entries, head, log: null }, code, platform);
}

/** The key ring of the package. */
function ring(keys: readonly PackageKey[]) {
  const map = new Map(keys.map((item) => [epochKey(item.scope, item.scope_id, item.epoch), item.key]));
  return (scope: string, scopeId: string, epoch: number): Secret | null =>
    map.get(epochKey(scope, scopeId, epoch)) ?? null;
}

/** The joining user: registered with this invitation or an existing account (section 5.2a). */
export interface Joiner {
  readonly userId: string;
  readonly identity: Identity;
}

/** A built `accept` batch and what the client keeps after it was stored (shape of `PreparedOrg`). */
export interface PreparedAccept {
  readonly org: string;
  readonly defaultWg: string;
  readonly body: Readonly<Record<string, unknown>>;
  /** The log after the `accept`. */
  readonly log: VerifiedLog;
  /** The package keys of the scopes the user reads after the `accept`. */
  readonly keys: ReadonlyMap<string, Secret>;
  /** Whether the batch carries the `member.profile` event (it needs the package key of the current org epoch). */
  readonly profile: boolean;
}

export interface AcceptInput {
  readonly invitation: OrgInvitation;
  readonly joiner: Joiner;
  readonly displayName: string;
  readonly platform: PlatformState | null;
  readonly now?: () => number;
}

/** The `member.profile` event of `joiner` in `org`, sealed with the org key of `epoch` at log head `lh`. */
export async function memberProfile(
  org: string,
  epoch: number,
  orgKey: Secret,
  joiner: Joiner,
  name: string,
  lh: number,
  at: number,
): Promise<unknown> {
  const header = newHeader({
    id: uuid7(at),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: joiner.userId,
    kind: 'member.profile',
    ts: formatTime(at),
    lh,
    ref: joiner.userId,
  });
  return sealEvent(orgKey, header, bodyJson({ display_name: name }), joiner.identity.sig.privateKey);
}

/** Builds `accept` (both signatures), the self-wraps of the current package keys and the `member.profile` event. */
export async function prepareAccept(input: AcceptInput): Promise<PreparedAccept> {
  const name = displayName(input.displayName) ?? fail('header_invalid');
  const { invitation, joiner } = input;
  const log = invitation.log ?? fail('log_empty');
  const now = input.now ?? Date.now;
  const ts = formatTime(now());
  const keys = ring(invitation.keys);
  const built = await buildBatch({
    log,
    author: { id: joiner.userId, identity: joiner.identity, invite: invitation.signing },
    entries: [
      {
        type: 'accept',
        invite_id: invitation.inviteId,
        subject: joiner.userId,
        kind: 'user',
        pub: joiner.identity.public(),
      },
    ],
    keys,
    platform: input.platform,
    ts,
  });
  const org = invitation.org;
  const state = built.log.state;
  const epoch = state.epoch(ORG, org);
  const orgKey = keys(ORG, org, epoch);
  const events =
    orgKey === null ? [] : [await memberProfile(org, epoch, orgKey, joiner, name, built.log.head.seq, now())];
  const held = new Map(
    invitation.keys
      .filter(
        (item) => (item.scope === ORG || item.scope === WG) && state.canRead(joiner.userId, item.scope, item.scope_id),
      )
      .map((item) => [epochKey(item.scope, item.scope_id, item.epoch), item.key]),
  );
  return {
    org,
    defaultWg: invitation.defaultWg,
    body: { ...built.body, events },
    log: built.log,
    keys: held,
    profile: events.length > 0,
  };
}

/** A signed request (`AppSession.client.request`). */
export type SignedRequest = (method: string, target: string, options?: { body: unknown }) => Promise<unknown>;

/** `POST /v1/orgs/{org}/log`; the answer must name the head of the built batch. */
export async function sendAccept(request: SignedRequest, prepared: PreparedAccept): Promise<void> {
  const answer = await request('POST', `/v1/orgs/${encodeURIComponent(prepared.org)}/log`, { body: prepared.body });
  requireHead(answer, prepared.log);
}

/** All pages of a log read (`GET /v1/orgs/{org}/log`, `GET /v1/platform/log`). */
async function readLog(request: SignedRequest, path: string): Promise<{ entries: unknown[]; head: Head | null }> {
  const entries: unknown[] = [];
  let after: number | null = null;
  for (;;) {
    const page = await request('GET', after === null ? path : `${path}?after=${after}`);
    if (!isRecord(page) || !Array.isArray(page.entries)) fail('header_invalid');
    entries.push(...page.entries.map((item: unknown) => (isRecord(item) ? item.entry : null)));
    if (!isSeq(page.next_after)) return { entries, head: isHead(page.head) ? page.head : null };
    after = page.next_after;
  }
}

/**
 * The verified platform state for the checks before `accept` (a log with platform devices, the excluded devices):
 * the platform log verified against the compiled root key; null while the platform log is empty. The log job stores
 * and pins it later as usual.
 */
export async function loadPlatform(request: SignedRequest, rootKey: string): Promise<PlatformState | null> {
  const { entries } = await readLog(request, '/v1/platform/log');
  return entries.length === 0 ? null : (await verifyPlatformLog(entries, rootKey)).state;
}

/**
 * The hub already stored an `accept` of this user (an answer that got lost): the log is read as a member and
 * verified like the lookup log; it must hold this invitation accepted by the user (`log_accept`).
 */
async function joinedLog(
  request: SignedRequest,
  prepared: PreparedAccept,
  invitation: OrgInvitation,
  input: AcceptInput,
): Promise<PreparedAccept> {
  const { entries, head } = await readLog(request, `/v1/orgs/${encodeURIComponent(invitation.org)}/log`);
  await checkExtends(invitation.packageHead, entries);
  const log = await verifyLog(entries, invitation.founder, input.platform);
  if (head === null || head.seq !== log.head.seq || head.hash !== log.head.hash) fail('log_chain');
  const accepted = log.state.invites.get(invitation.inviteId)?.acceptedSeq ?? null;
  const entry = accepted === null ? null : log.entries[accepted]!.body;
  if (entry?.subject !== input.joiner.userId || log.state.member(input.joiner.userId) === null) fail('log_accept');
  return { ...prepared, log };
}

/** Attempts of `joinOrg` (one build plus rebuilds after `seq_conflict`). */
export const ACCEPT_ATTEMPTS = 3;

export interface JoinDeps {
  readonly anon: AnonRequest;
  readonly request: SignedRequest;
  /** Whether the signed-in user is a member of `org` now (`GET /v1/me`). */
  readonly isMember: (org: string) => Promise<boolean>;
}

/**
 * Builds and sends the `accept`; after any failure a stored `accept` (lost answer, also of an earlier attempt) counts as
 * joined once the verified log shows it; otherwise after `seq_conflict` the log is re-read through a new lookup and the
 * batch rebuilt (at most `ACCEPT_ATTEMPTS` builds).
 */
export async function joinOrg(
  deps: JoinDeps,
  input: AcceptInput & { readonly code: string },
): Promise<{ prepared: PreparedAccept; invitation: OrgInvitation }> {
  let invitation = input.invitation;
  for (let attempt = 1; ; attempt += 1) {
    const prepared = await prepareAccept({ ...input, invitation });
    try {
      await sendAccept(deps.request, prepared);
      return { prepared, invitation };
    } catch (error) {
      // a stored `accept` whose answer got lost (hub restarted, KOR-36) or that won a race: the membership says so and
      // the verified log must hold this invitation accepted by the user
      if (await deps.isMember(prepared.org).catch(() => false)) {
        return { prepared: await joinedLog(deps.request, prepared, invitation, input), invitation };
      }
      if (!(error instanceof HubError) || error.code !== 'seq_conflict' || attempt === ACCEPT_ATTEMPTS) throw error;
      invitation = await refreshInvitation(deps.anon, invitation, input.code, input.platform);
    }
  }
}
