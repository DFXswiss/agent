// Org invitations made in the browser (06-web.md section 7.1 "Invite user", 04-crypto.md sections 4.6, 5.7, ADR-005;
// 03-api.md section 4.2). Exported for the device screen (WEB-07: device invitations use `createInvite` with
// `kind: 'device'`).
//
// The admin's browser draws a 16-byte code and derives `invite_id`, `pk_inv` and `package_key`; it appends the
// `invite` entry and uploads the sealed package in one `POST /v1/orgs/{org}/invites`. The package carries the org name,
// the names of the invited workgroups, the pinned founder, the head **after** the `invite` entry, the hub key and the
// current keys: `OK` and the `WK` of every invited workgroup (users: plus the default workgroup). The admin must hold
// those keys (`epoch_stale` otherwise), so the screens offer only workgroups whose current key the admin holds. On
// `seq_conflict` the entry and the package are rebuilt on the re-read log with the same code. The code is returned
// once (link `<origin>/invite#<code>` for users) and never stored or sent. A user invitation may carry the inviter's
// personal message (rule E-28, KOR-64) in the package; the invitee sees it where it joins.
//
// Revocation appends `invite_revoke` (`POST …/invites/{id}/revoke`); an invitation that is no longer open in the
// re-read log is done.
import { b64uEncode } from '../encoding/b64u';
import { formatTime } from '../encoding/time';
import { isMessageText } from '../crypto/agent';
import { formatCode, generateCode, inviteSecrets, sealPackage } from '../crypto/invite';
import { buildEntries, type LogFields } from '../log/entries';
import { ORG, type OrgState, WG } from '../log/state';
import { extendLog } from '../log/verify';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { setLog } from '../store/slices/orgs';
import type { Directory } from '../tasks/directory';
import { type Actor, keyRing, type OrgContext, refuse, verifiedPlatform, withLogRetry } from './batch';
import type { Access, Role } from './members';

export const HOUR_MS = 3_600_000;
/** Selectable expiries in hours (1 h – 30 d, ADR-005 / 03-api.md section 12). */
export const EXPIRY_HOURS = [1, 6, 24, 72, 168, 336, 720] as const;
/** Defaults: user invitations 72 h, device invitations 24 h. */
export const USER_EXPIRY_HOURS = 72;
export const DEVICE_EXPIRY_HOURS = 24;

/** What the admin chooses. */
export type InviteRequest =
  | {
      readonly kind: 'user';
      readonly role: Role;
      /** Further workgroups (the default workgroup is always included with write). */
      readonly wgs: Readonly<Record<string, Access>>;
      readonly hours: number;
      /** The inviter's personal message for the invitee (rule E-28), sealed in the package; absent: none. */
      readonly message?: string;
    }
  | {
      readonly kind: 'device';
      /** The workgroups the device is assigned to. */
      readonly wgs: readonly string[];
      readonly hours: number;
    };

/** A created invitation: shown once, then forgotten. */
export interface CreatedInvite {
  readonly inviteId: string;
  /** `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`. */
  readonly code: string;
  /** `<origin>/invite#<code>` (users; devices type the code into `agent-runner enroll`). */
  readonly link: string;
  readonly expires: string;
  readonly seq: number;
}

/** What `createInvite` needs besides the context. */
export interface InviteInput {
  readonly request: InviteRequest;
  /** Names for the package (`org_name`, `wg_names`) from the verified profiles. */
  readonly directory: Directory;
  /** The hub key of `/v1/config` (the invitee pins it). */
  readonly hubKey: { readonly id: string; readonly public_key: string };
  /** The page origin of the link. */
  readonly origin: string;
  /** Source of the code (default: 16 random bytes). */
  readonly code?: () => Uint8Array;
}

/** Whether the user may invite into `wg`: a writable workgroup whose current key the user holds. */
export function invitable(state: OrgState, held: (scope: string, id: string, epoch: number) => unknown, wg: string) {
  return state.writable(wg) && held(WG, wg, state.epoch(WG, wg)) !== null;
}

/** The log fields of the `invite` entry. */
export function inviteFields(
  state: OrgState,
  request: InviteRequest,
  inviteId: string,
  pkInv: string,
  expires: string,
): LogFields {
  if (request.kind === 'device') {
    if (request.wgs.length === 0) refuse('params_invalid');
    const wgs = Object.fromEntries(request.wgs.map((wg) => [wg, 'device' as const]));
    return { type: 'invite', invite_id: inviteId, pk_inv: pkInv, kind: 'device', wgs, expires };
  }
  const wgs = Object.fromEntries(Object.entries(request.wgs).filter(([wg]) => wg !== state.defaultWg));
  return { type: 'invite', invite_id: inviteId, pk_inv: pkInv, kind: 'user', role: request.role, wgs, expires };
}

/** The workgroups whose keys the package carries: the invited ones (users: plus the default workgroup), sorted. */
export function packageWorkgroups(state: OrgState, request: InviteRequest): string[] {
  const chosen = request.kind === 'device' ? request.wgs : [state.defaultWg, ...Object.keys(request.wgs)];
  return [...new Set(chosen)].sort();
}

function checkExpiry(hours: number): void {
  if (!Number.isInteger(hours) || hours < 1 || hours > 720) refuse('params_invalid');
}

async function attemptInvite(context: OrgContext, org: string, input: InviteInput, code: Uint8Array, actor: Actor) {
  const { request } = input;
  const state = actor.log.state;
  if (!state.isAdmin(actor.userId)) refuse('forbidden');
  const secrets = await inviteSecrets(code);
  const expires = formatTime(context.now() + request.hours * HOUR_MS);
  const fields = inviteFields(state, request, secrets.inviteId, secrets.pkInv, expires);
  const wgs = packageWorkgroups(state, request);
  const held = keyRing(context.store, org);
  if (!wgs.every((wg) => invitable(state, held, wg))) refuse('epoch_stale');
  const orgEpoch = state.epoch(ORG, org);
  const orgKey = held(ORG, org, orgEpoch) ?? refuse('epoch_stale');
  const signer = { id: actor.userId, key: actor.identity.sig.privateKey };
  const [entry] = await buildEntries({ org, head: actor.log.head }, signer, [fields], formatTime(context.now()));
  const after = await extendLog(actor.log, [entry], verifiedPlatform(context.store));
  const keys = [
    { scope: ORG, scope_id: org, epoch: orgEpoch, key: orgKey.reveal((raw) => b64uEncode(raw)) },
    ...wgs.map((wg) => {
      const epoch = state.epoch(WG, wg);
      return { scope: WG, scope_id: wg, epoch, key: held(WG, wg, epoch)!.reveal((raw) => b64uEncode(raw)) };
    }),
  ];
  const pkg = {
    org,
    org_name: input.directory.orgName ?? '',
    wg_names: Object.fromEntries(wgs.map((wg) => [wg, input.directory.workgroups.get(wg) ?? ''])),
    founder: state.founder,
    head: { seq: after.head.seq, hash: after.head.hash },
    keys,
    hub_key: { id: input.hubKey.id, public_key: input.hubKey.public_key },
    ...(request.kind === 'user' && request.message !== undefined ? { message: request.message } : {}),
    v: 1,
  };
  const packageCt = await sealPackage(secrets, pkg);
  const answer = await context.client.request('POST', `/v1/orgs/${encodeURIComponent(org)}/invites`, {
    body: { entry, package_ct: packageCt },
  });
  if (!isRecord(answer) || answer.invite_id !== secrets.inviteId || answer.seq !== after.head.seq) {
    throw new HubError('unavailable', 200);
  }
  setLog(context.store, org, after);
  const text = formatCode(code);
  return {
    inviteId: secrets.inviteId,
    code: text,
    link: `${input.origin}/invite#${text}`,
    expires,
    seq: after.head.seq,
  };
}

/** Creates an invitation (`invite` entry + sealed package); retried on `seq_conflict` with the same code. */
export async function createInvite(context: OrgContext, org: string, input: InviteInput): Promise<CreatedInvite> {
  checkExpiry(input.request.hours);
  const { request } = input;
  if (request.kind === 'user' && request.message !== undefined && !isMessageText(request.message)) {
    refuse('params_invalid');
  }
  const code = (input.code ?? generateCode)();
  return withLogRetry(context, org, (actor) => attemptInvite(context, org, input, code, actor));
}

/** One row of `GET /v1/orgs/{org}/invites`. */
export interface InviteRow {
  readonly invite_id: string;
  readonly kind: 'user' | 'device';
  readonly state: 'open' | 'used' | 'revoked' | 'expired';
  readonly created_at: string;
  readonly expires_at: string;
  readonly created_by: string;
  /** Whether a person registered a passkey with it (the join may still be missing). */
  readonly registered?: boolean;
  /** First lookup of the invitation (hubs since KOR-36; null: never opened, absent: older hub). */
  readonly opened_at?: string | null;
}

const STATES = ['open', 'used', 'revoked', 'expired'];

const isInviteRow = (value: unknown): value is InviteRow =>
  isRecord(value) &&
  typeof value.invite_id === 'string' &&
  (value.kind === 'user' || value.kind === 'device') &&
  STATES.includes(value.state as string) &&
  typeof value.created_at === 'string' &&
  typeof value.expires_at === 'string' &&
  typeof value.created_by === 'string';

/**
 * The open invitations of `kind`, newest first. The hub's list is a hint: only invitations the verified log knows as
 * open (not revoked, not accepted) are listed, with the expiry of the signed entry.
 */
export async function openInvites(
  context: OrgContext,
  org: string,
  kind: 'user' | 'device',
): Promise<(InviteRow & { readonly seq: number })[]> {
  const body = await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/invites`);
  if (!isRecord(body) || !Array.isArray(body.invites) || !body.invites.every(isInviteRow)) {
    throw new HubError('unavailable', 200);
  }
  const state = context.store.getState().orgs.byId[org]?.log?.state ?? refuse('log_empty');
  const now = context.now();
  return body.invites
    .flatMap((row) => {
      const known = state.invites.get(row.invite_id);
      const open =
        known !== undefined &&
        known.kind === kind &&
        known.revokedSeq === null &&
        known.acceptedSeq === null &&
        Date.parse(known.expires) > now;
      return row.state === 'open' && open ? [{ ...row, expires_at: known.expires, seq: known.seq }] : [];
    })
    .sort((a, b) => b.seq - a.seq);
}

/**
 * Where an invitation stands for the inviter (KOR-36): `accepted` (the verified log has the accept), `registered` (a
 * person created an account with it, the join is missing), `opened` (the link was opened), `open`.
 */
export type InviteStatus = 'open' | 'opened' | 'registered' | 'accepted';

export interface InviteStatusRow {
  readonly inviteId: string;
  readonly status: InviteStatus;
  readonly expires: string;
  readonly seq: number;
  /** The member who joined with it (from the verified log: the principal admitted by the accept), for `accepted`. */
  readonly acceptedBy: string | undefined;
  readonly openedAt: string | null;
}

/**
 * The user invitations of the org with their state, newest first: open ones (not revoked, not expired) and accepted
 * ones that have not expired yet ("did they accept?"). The verified log decides which invitations exist and whether
 * they were accepted; the hub's list only adds what the log cannot know (opened, registered) and is a hint.
 */
export async function inviteStatuses(context: OrgContext, org: string): Promise<InviteStatusRow[]> {
  const body = await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/invites`);
  if (!isRecord(body) || !Array.isArray(body.invites) || !body.invites.every(isInviteRow)) {
    throw new HubError('unavailable', 200);
  }
  const state = context.store.getState().orgs.byId[org]?.log?.state ?? refuse('log_empty');
  const now = context.now();
  const rows = new Map(body.invites.map((row) => [row.invite_id, row]));
  const joined = new Map([...state.principals.values()].map((p) => [p.joinedSeq, p.id]));
  return [...state.invites.values()]
    .flatMap((known): InviteStatusRow[] => {
      if (known.kind !== 'user' || known.revokedSeq !== null || Date.parse(known.expires) <= now) return [];
      const row = rows.get(known.inviteId);
      const accepted = known.acceptedSeq !== null;
      // an open invitation the hub does not list as open (used, revoked, unknown) is not shown
      if (!accepted && row?.state !== 'open') return [];
      const status: InviteStatus = accepted
        ? 'accepted'
        : row!.registered === true
          ? 'registered'
          : typeof row!.opened_at === 'string'
            ? 'opened'
            : 'open';
      return [
        {
          inviteId: known.inviteId,
          status,
          expires: known.expires,
          seq: known.seq,
          acceptedBy: accepted ? joined.get(known.acceptedSeq!) : undefined,
          openedAt: typeof row?.opened_at === 'string' ? row.opened_at : null,
        },
      ];
    })
    .sort((a, b) => b.seq - a.seq);
}

/** Revokes an open invitation; done if the re-read log no longer has it open. */
export async function revokeInvite(context: OrgContext, org: string, inviteId: string): Promise<void> {
  await withLogRetry(context, org, async (actor) => {
    const state = actor.log.state;
    if (!state.isAdmin(actor.userId)) refuse('forbidden');
    const known = state.invites.get(inviteId) ?? refuse('not_found');
    if (known.revokedSeq !== null || known.acceptedSeq !== null) return;
    const signer = { id: actor.userId, key: actor.identity.sig.privateKey };
    const fields: LogFields = { type: 'invite_revoke', invite_id: inviteId };
    const [entry] = await buildEntries({ org, head: actor.log.head }, signer, [fields], formatTime(context.now()));
    const after = await extendLog(actor.log, [entry], verifiedPlatform(context.store));
    const target = `/v1/orgs/${encodeURIComponent(org)}/invites/${encodeURIComponent(inviteId)}/revoke`;
    const answer = await context.client.request('POST', target, { body: { entry } });
    if (!isRecord(answer) || answer.seq !== after.head.seq) throw new HubError('unavailable', 200);
    setLog(context.store, org, after);
  });
}
