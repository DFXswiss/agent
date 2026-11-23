// Direct admission of an existing account (06-web.md section 7.1 "Add member", 04-crypto.md section 5.13, ADR-060).
//
// The admin's browser finds the account by its account code (`GET /v1/users/lookup?code=`) or picks one of the accounts
// registered through its own platform invitations (`GET /v1/me/invitees`). Every identity answer of the hub is checked
// against the platform log the browser verified with the compiled root key (`PlatformState.identityAnchored`, at the
// hub's receipt time: a certification must still be valid); a typed code must also be the code of exactly the proven
// keys. The browser then appends `admit` with its own anchor (`GET /v1/users/{me}/identity`, which must name its own
// unlocked keys) through `submitBatch`: the batch wraps the current org key, the default-workgroup key and every listed
// workgroup key the admin holds for the new member; the remaining keys arrive by catch-up. The admitted person does
// nothing; it finds the org and a notification `org.admitted`.
import { accountCode, parseAccountCode } from '../crypto/identity';
import { CryptoError } from '../crypto/errors';
import type { PlatformState } from '../log/platform';
import { ADMIN, MEMBER } from '../log/state';
import type { SignedClient } from '../net/signedFetch';
import { isRecord } from '../registry';
import { type OrgContext, refuse, submitBatch, type SubmitProgress, type Submitted, verifiedPlatform } from './batch';
import type { Access, Role } from './members';

/** A proven identity of an account (public keys and their anchor in the platform log). */
export interface ProvenIdentity {
  readonly userId: string;
  readonly pub: { readonly kem: string; readonly sig: string };
  readonly platformSeq: number;
  readonly idSig: string;
  /** The 12 characters of the account code, recomputed from the proven keys. */
  readonly accountCode: string;
}

/** An account registered through one of the admin's own platform invitations. */
export interface Invitee {
  readonly userId: string;
  readonly registeredAt: string;
  /** Null while the account cannot be admitted directly (no anchor yet, or the answer did not verify). */
  readonly identity: ProvenIdentity | null;
}

/** What `findAccount` looks for: an account code as typed, or a user ID. */
export type AccountQuery = { readonly code: string } | { readonly user: string };

const isText = (value: unknown): value is string => typeof value === 'string';
const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

/**
 * The identity answer `answer` checked against the platform log: the anchor at `platform_seq` proves `(user_id, pub)`
 * (at receipt time), `user_id` is `user` if given, and a typed `code` is the code of these keys; `identity_unproven`
 * otherwise.
 */
export async function proveIdentity(
  answer: unknown,
  platform: PlatformState,
  expected: { readonly user?: string; readonly code?: string } = {},
): Promise<ProvenIdentity> {
  const pub = isRecord(answer) && isRecord(answer.pub) ? answer.pub : null;
  if (
    !isRecord(answer) ||
    pub === null ||
    !isText(answer.user_id) ||
    !isText(pub.kem) ||
    !isText(pub.sig) ||
    !isSeq(answer.platform_seq) ||
    !isText(answer.id_sig)
  )
    refuse('identity_unproven');
  const keys = { kem: pub.kem, sig: pub.sig };
  const userId = answer.user_id;
  if (expected.user !== undefined && userId !== expected.user) refuse('identity_unproven');
  let anchored: boolean;
  let code: string;
  try {
    anchored = await platform.identityAnchored(answer.platform_seq, userId, keys, answer.id_sig, true);
    code = await accountCode(userId, keys);
  } catch (error) {
    if (!(error instanceof CryptoError)) throw error;
    refuse('identity_unproven');
  }
  if (!anchored || (expected.code !== undefined && code !== expected.code)) refuse('identity_unproven');
  return { userId, pub: keys, platformSeq: answer.platform_seq, idSig: answer.id_sig, accountCode: code };
}

function platformOf(context: OrgContext): PlatformState {
  return verifiedPlatform(context.store) ?? refuse('identity_unproven');
}

/** The proven identity of the account named by `query` (`account_code_invalid` for a malformed code). */
export async function findAccount(
  client: Pick<SignedClient, 'request'>,
  platform: PlatformState,
  query: AccountQuery,
): Promise<ProvenIdentity> {
  if ('user' in query) {
    const answer = await client.request('GET', `/v1/users/${encodeURIComponent(query.user)}/identity`);
    return proveIdentity(answer, platform, { user: query.user });
  }
  let code: string;
  try {
    code = parseAccountCode(query.code);
  } catch {
    refuse('account_code_invalid');
  }
  const answer = await client.request('GET', `/v1/users/lookup?code=${code}`);
  return proveIdentity(answer, platform, { code });
}

/** The accounts registered through the admin's own platform invitations, each with its proven identity or null. */
export async function loadInvitees(context: OrgContext): Promise<Invitee[]> {
  const platform = platformOf(context);
  const answer = await context.client.request('GET', '/v1/me/invitees');
  const items = isRecord(answer) && Array.isArray(answer.invitees) ? answer.invitees : [];
  const rows: Invitee[] = [];
  for (const item of items) {
    if (!isRecord(item) || !isText(item.user_id) || !isText(item.registered_at)) continue;
    let identity: ProvenIdentity | null = null;
    if (item.identity !== null) {
      try {
        identity = await proveIdentity(item.identity, platform, { user: item.user_id });
      } catch {
        identity = null;
      }
    }
    rows.push({ userId: item.user_id, registeredAt: item.registered_at, identity });
  }
  return rows;
}

/** Admits `subject` (module comment); null once it is a member already (another admin was faster). */
export async function admitAccount(
  context: OrgContext,
  org: string,
  subject: ProvenIdentity,
  role: Role,
  wgs: Readonly<Record<string, Access>>,
  progress?: (step: SubmitProgress) => void,
): Promise<Submitted> {
  if (role !== ADMIN && role !== MEMBER) refuse('forbidden');
  const platform = platformOf(context);
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const own = await findAccount(context.client, platform, { user: userId });
  const mine = identity.public();
  if (own.pub.kem !== mine.kem || own.pub.sig !== mine.sig) refuse('identity_unproven');
  return submitBatch(
    context,
    org,
    (log, me) => {
      const state = log.state;
      if (!state.isAdmin(me)) refuse('forbidden');
      if (state.member(subject.userId) !== null) return null;
      const granted = Object.fromEntries(Object.entries(wgs).filter(([wg]) => wg !== state.defaultWg));
      if (Object.keys(granted).some((wg) => !state.writable(wg))) refuse('forbidden');
      return {
        entries: [
          {
            type: 'admit',
            subject: subject.userId,
            pub: { kem: subject.pub.kem, sig: subject.pub.sig },
            platform_seq: subject.platformSeq,
            id_sig: subject.idSig,
            by_platform_seq: own.platformSeq,
            by_id_sig: own.idSig,
            role,
            wgs: granted,
          },
        ],
      };
    },
    progress,
  );
}

/** The user's own account code (12 characters), computed from the unlocked identity – never from the hub. */
export async function ownAccountCode(context: Pick<OrgContext, 'store'>): Promise<string | null> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) return null;
  return accountCode(userId, identity.public());
}
