// Platform invitations of inviters (06-web.md section 7.4; 03-api.md section 4.2; 04-crypto.md sections 3, 5.3;
// ADR-004, ADR-005, ADR-026).
//
// A user with the platform role `inviter`, or every user while the switch `open_platform_invites` is on (ADR-078) – read
// from the verified platform log (`role_grant`/`role_revoke`, `platform_policy`, compiled root key), never from the
// hub's word – creates platform user invitations for customers: the browser draws the
// 16-byte code, derives `invite_id` and `pk_inv` and sends only them and the expiry (`POST /v1/platform/invites`;
// `pk_inv` lets only the code's holder register through the invitation, ADR-043); the code is
// returned once (link `<origin>/invite#<code>`) and never stored or sent. A platform invitation has no package: the
// invitee registers and creates its private org with platform access (ADR-026).
//
// `preparePlatformInvite` draws the code and fixes the expiry once, so that a retry after a lost answer sends the
// identical request (the hub answers it like the first one, 03-api.md section 1). The list shows open, used, revoked
// and expired invitations (newest first, as the hub orders them); open ones can be revoked.
import { formatTime } from '../encoding/time';
import { formatCode, generateCode, inviteSecrets } from '../crypto/invite';
import type { PlatformState } from '../log/platform';
import { HubError, type SignedClient } from '../net/signedFetch';
import { refuse } from '../org/batch';
import { EXPIRY_HOURS, HOUR_MS, USER_EXPIRY_HOURS } from '../org/invites';
import { isRecord } from '../registry';

export const INVITER = 'inviter';
/** The hub allows at most 30 days from its own clock; the longest choice keeps this margin for clock skew. */
export const MAX_HOURS = 720;
export const SKEW_MS = 120_000;
/** Selectable expiries (1 h – 30 d) and the default (user invitations: 72 h, ADR-005). */
export { EXPIRY_HOURS, USER_EXPIRY_HOURS };

type Client = Pick<SignedClient, 'request'>;

/** The switch that lets every user invite (ADR-078). */
export const OPEN_INVITES = 'open_platform_invites';

/**
 * The user may create platform invitations as the verified platform log says (false while it is not verified): the
 * platform role `inviter`, or anybody while the switch `open_platform_invites` is on (ADR-078; without the role the
 * hub lists and revokes only the user's own invitations).
 */
export function isInviter(platform: PlatformState | null, userId: string | null): boolean {
  return platform !== null && userId !== null && (platform.hasRole(userId, INVITER) || platform.policy(OPEN_INVITES));
}

/** A drawn invitation, ready to send (keep it to retry with the same ID and expiry). */
export interface PreparedPlatformInvite {
  readonly inviteId: string;
  /** b64u public key of `sk_inv` (ADR-043). */
  readonly pkInv: string;
  /** `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`. */
  readonly code: string;
  /** `<origin>/invite#<code>`. */
  readonly link: string;
  readonly expires: string;
}

/** Draws the code (default: 16 random bytes) and fixes the expiry (`params_invalid` outside 1 h – 30 d). */
export async function preparePlatformInvite(input: {
  readonly hours: number;
  readonly now: number;
  readonly origin: string;
  readonly code?: () => Uint8Array;
}): Promise<PreparedPlatformInvite> {
  const { hours } = input;
  if (!Number.isInteger(hours) || hours < 1 || hours > MAX_HOURS) refuse('params_invalid');
  const raw = (input.code ?? generateCode)();
  const text = formatCode(raw);
  const secrets = await inviteSecrets(raw);
  return {
    inviteId: secrets.inviteId,
    pkInv: secrets.pkInv,
    code: text,
    link: `${input.origin}/invite#${text}`,
    expires: formatTime(input.now + Math.min(hours * HOUR_MS, MAX_HOURS * HOUR_MS - SKEW_MS)),
  };
}

/** `POST /v1/platform/invites` with the ID, `pk_inv` and the expiry only (the code never leaves the browser). */
export async function createPlatformInvite(client: Client, prepared: PreparedPlatformInvite): Promise<void> {
  const answer = await client.request('POST', '/v1/platform/invites', {
    body: { invite_id: prepared.inviteId, pk_inv: prepared.pkInv, kind: 'user', expires_at: prepared.expires },
  });
  if (!isRecord(answer)) throw new HubError('unavailable', 200);
}

export type PlatformInviteState = 'open' | 'used' | 'revoked' | 'expired';

/** One row of `GET /v1/platform/invites`. */
export interface PlatformInviteRow {
  readonly invite_id: string;
  readonly kind: 'user';
  readonly state: PlatformInviteState;
  readonly created_by: string | null;
  readonly created_at: string;
  readonly expires_at: string;
  readonly registered: boolean;
  readonly used_at: string | null;
  readonly revoked_at: string | null;
}

const STATES: readonly string[] = ['open', 'used', 'revoked', 'expired'];
const timeOrNull = (value: unknown) => value === null || typeof value === 'string';

const isRow = (value: unknown): value is PlatformInviteRow =>
  isRecord(value) &&
  typeof value.invite_id === 'string' &&
  value.kind === 'user' &&
  STATES.includes(value.state as string) &&
  (value.created_by === null || typeof value.created_by === 'string') &&
  typeof value.created_at === 'string' &&
  typeof value.expires_at === 'string' &&
  typeof value.registered === 'boolean' &&
  timeOrNull(value.used_at) &&
  timeOrNull(value.revoked_at);

/** The platform invitations, newest first (`unavailable` for a malformed answer). */
export async function listPlatformInvites(client: Client): Promise<PlatformInviteRow[]> {
  const body = await client.request('GET', '/v1/platform/invites');
  if (!isRecord(body) || !Array.isArray(body.invites) || !body.invites.every(isRow)) {
    throw new HubError('unavailable', 200);
  }
  return body.invites;
}

/** Revokes an open platform invitation (repeating is a no-op at the hub; a used one: `invite_consumed`). */
export async function revokePlatformInvite(client: Client, id: string): Promise<void> {
  await client.request('POST', `/v1/platform/invites/${encodeURIComponent(id)}/revoke`, { body: {} });
}
