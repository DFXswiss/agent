// The user's credential records (06-web.md section 8.2, 03-api.md section 4.8, 04-crypto.md section 4.4): build them
// under the user's current credential key `CK`, store, list, open and delete them. Everything confidential is
// encrypted and opened here; only the non-secret summary of a record (`summarize`) leaves this module – tokens, API
// keys and passwords never reach the store or the UI.
//
// - `ownKeys`/`listOwnKeys`: the user's own `CK` self-wraps (`GET /v1/wraps?scope=cred&scope_id=<me>`): wrapped for the
//   user by the user or its active agent (signature checked with that key), opened with the own X25519 key; newest
//   epoch first, with the hub's `created_at` of the wrap (the start of the epoch, used by the rotation job). An epoch
//   only a former agent of the user wrapped (ADR-082 section 9.5) is taken for opening existing records alone
//   (`opening`: listing them and the rotation that re-encrypts them under a new epoch the user creates), never for new
//   records nor as the current epoch. Wraps that do not verify or open, or of a stranger, are skipped.
// - `sealRecord`: the record of a body (`crypto/credential.ts`): header `{v, alg, id, owner, kind, version, epoch,
//   author, ts}` + fresh salt and nonce, encrypted under `CK_epoch`, signed by the author.
// - `openRecord`: owner = the user, kind as asked, author the user, a device the verified logs entitle to the user's
//   `CK` or the user's active, not revoked agent (ADR-082 section 9.5; a caller may widen this, `AuthorKeys`), signature, `CK` of the record's epoch, and a body exactly as
//   `contracts/credentials/<kind>.json` allows it (`params_invalid` otherwise).
// - `putCredential`: `PUT /v1/me/credentials/{kind}` with `expected_version` (0 = create; an update keeps the stored
//   record ID, HUB-13); a `409 version_conflict` (another browser or device wrote in between) is retried once with
//   the stored version.
// - `deleteCredential`: `DELETE /v1/me/credentials/{kind}`; `404` counts as done.
// - `loadCredentials`: the list (`GET /v1/me/credentials`) with every record opened and summarized, into the
//   `credentials` slice.
import { uuid7 } from '../encoding/ids';
import { bodyJson, parseJson } from '../encoding/json';
import { formatTime, parseTime } from '../encoding/time';
import { CryptoError } from '../crypto/errors';
import { KINDS, newHeader, openCredential, sealCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import type { Sealed } from '../crypto/signed';
import { unwrapKey, verifyWrap } from '../crypto/wrap';
import { authorKey, formerAgentKey, isGithubBody, selfWrapperKey } from '../github/connection';
import { CRED } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { EMAIL_RE } from '../requests/bodies';
import { type RequestsContext, RequestError } from '../requests/client';
import { type CredentialEntry, setCredentials } from '../store/slices/credentials';

export type CredentialKind = 'github' | 'anthropic_api_key' | 'claude_subscription' | 'mailbox';
export const CREDENTIAL_KINDS = KINDS as readonly CredentialKind[];

export const isCredentialKind = (value: unknown): value is CredentialKind =>
  (CREDENTIAL_KINDS as readonly unknown[]).includes(value);

function refuse(code: string): never {
  throw new RequestError(code);
}

// --- bodies (contracts/credentials/*.json) ---

const PRINTABLE_RE = /^[!-~]{1,4096}$/;
const HOST_RE = /^[A-Za-z0-9.-]{1,253}$/;
const IMAP_USER_RE = /^[ -~]{1,256}$/;
const MAILBOX_KEYS = ['imap_host', 'imap_port', 'imap_user', 'imap_password', 'email', 'name', 'purpose', 'use'];

const keysAre = (value: Record<string, unknown>, keys: readonly string[]): boolean =>
  Object.keys(value).sort().join(',') === [...keys].sort().join(',');

export const isEmail = (value: unknown): value is string =>
  typeof value === 'string' && value.length <= 254 && EMAIL_RE.test(value);

export interface ApiKeyBody {
  readonly key: string;
}

export interface MailboxBody {
  readonly imap_host: string;
  readonly imap_port: number;
  readonly imap_user: string;
  readonly imap_password: string;
  readonly email: string;
  /** KOR-74 (ADR-099 section 2): optional; checked by `core/credentials/mailboxes.ts`. */
  readonly name?: string;
  readonly purpose?: string;
  readonly use?: { readonly wgs: readonly string[] };
}

/** `setup_token`: a long-lived token of `claude setup-token`; `login_token`: the access token of the local Claude Code
 *  login, transferred by the user's agent tool, with its expiry (ADR-056 section 7). */
export const SUBSCRIPTION_ORIGINS = ['setup_token', 'login_token'] as const;
export type SubscriptionOrigin = (typeof SUBSCRIPTION_ORIGINS)[number];

export interface SubscriptionBody {
  readonly token: string;
  readonly email: string | null;
  readonly kind: SubscriptionOrigin;
  readonly expires_at?: string;
}

export const isApiKeyBody = (value: unknown): value is ApiKeyBody =>
  isRecord(value) && keysAre(value, ['key']) && typeof value.key === 'string' && PRINTABLE_RE.test(value.key);

export const isMailboxBody = (value: unknown): value is MailboxBody =>
  isRecord(value) &&
  // name, purpose and release of KOR-74 are checked in full by `core/credentials/mailboxes.ts` (ADR-099 section 2)
  Object.keys(value).every((key) => MAILBOX_KEYS.includes(key)) &&
  MAILBOX_KEYS.slice(0, 5).every((key) => key in value) &&
  typeof value.imap_host === 'string' &&
  HOST_RE.test(value.imap_host) &&
  Number.isSafeInteger(value.imap_port) &&
  (value.imap_port as number) >= 1 &&
  (value.imap_port as number) <= 65535 &&
  typeof value.imap_user === 'string' &&
  IMAP_USER_RE.test(value.imap_user) &&
  typeof value.imap_password === 'string' &&
  [...value.imap_password].length >= 1 &&
  [...value.imap_password].length <= 1024 &&
  isEmail(value.email);

const isHeaderTime = (value: unknown): boolean => {
  try {
    parseTime(value);
    return true;
  } catch {
    return false;
  }
};

export const isSubscriptionBody = (value: unknown): value is SubscriptionBody =>
  isRecord(value) &&
  (Object.hasOwn(value, 'expires_at')
    ? keysAre(value, ['token', 'email', 'kind', 'expires_at']) && isHeaderTime(value.expires_at)
    : keysAre(value, ['token', 'email', 'kind'])) &&
  typeof value.token === 'string' &&
  PRINTABLE_RE.test(value.token) &&
  (value.email === null || isEmail(value.email)) &&
  (SUBSCRIPTION_ORIGINS as readonly unknown[]).includes(value.kind);

export const MAILBOX_FIELDS = ['imap_host', 'imap_port', 'imap_user', 'imap_password', 'email'] as const;
export type MailboxForm = Readonly<Record<(typeof MAILBOX_FIELDS)[number], string>>;

/** The `mailbox` body of a form (texts trimmed except the password, port as a number), or null if not allowed. */
export function mailboxBody(values: MailboxForm): MailboxBody | null {
  const port = values.imap_port.trim();
  const body = {
    imap_host: values.imap_host.trim(),
    imap_port: /^[0-9]{1,5}$/.test(port) ? Number(port) : 0,
    imap_user: values.imap_user.trim(),
    imap_password: values.imap_password,
    email: values.email.trim(),
  };
  return isMailboxBody(body) ? body : null;
}

const BODY_CHECKS: Readonly<Record<CredentialKind, (value: unknown) => boolean>> = {
  github: (value) => isGithubBody(value) && Number.isFinite(Date.parse(value.created_at)),
  anthropic_api_key: isApiKeyBody,
  claude_subscription: isSubscriptionBody,
  mailbox: isMailboxBody,
};

/** Whether `body` is exactly what `contracts/credentials/<kind>.json` allows. */
export function validBody(kind: CredentialKind, body: unknown): boolean {
  return BODY_CHECKS[kind](body);
}

/** What of an opened record may be shown: never a token, key or password. */
export type Summary =
  | { readonly kind: 'github'; readonly login: string }
  | { readonly kind: 'anthropic_api_key' }
  | {
      readonly kind: 'claude_subscription';
      readonly email: string | null;
      readonly origin: SubscriptionOrigin;
      /** Header time of the token's expiry, null when the record states none. */
      readonly expiresAt: string | null;
    }
  | { readonly kind: 'mailbox'; readonly email: string; readonly host: string };

/** The non-secret part of a checked body (`validBody`). */
export function summarize(kind: CredentialKind, body: unknown): Summary {
  const value = body as Record<string, unknown>;
  switch (kind) {
    case 'github':
      return { kind, login: value.login as string };
    case 'anthropic_api_key':
      return { kind };
    case 'claude_subscription':
      return {
        kind,
        email: value.email as string | null,
        origin: value.kind as SubscriptionOrigin,
        expiresAt: (value.expires_at as string | undefined) ?? null,
      };
    case 'mailbox':
      return { kind, email: value.email as string, host: value.imap_host as string };
  }
}

// --- the credential key ---

export interface OwnKey {
  readonly epoch: number;
  readonly key: Secret;
  /** Unix ms of the hub's `created_at` of the self-wrap (null when the hub gave none). */
  readonly createdAt: number | null;
}

const createdAt = (value: unknown): number | null => {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
};

/** The user's `CK`s as `listOwnKeys` finds them. */
export interface OwnKeys {
  /** Self-wraps by the user or its active agent, newest epoch first: the epochs the browser builds on. */
  readonly keys: OwnKey[];
  /** `keys` and the epochs only a former agent of the user wrapped: for opening existing records, never for new ones
   *  and never as the start of the current epoch (ADR-082 section 9.5). */
  readonly opening: OwnKey[];
  /** The newest epoch of any self-wrap the hub lists (also one this browser does not take; 0 without one). */
  readonly listed: number;
}

/** The user's `CK` self-wraps that verify and open, by their wrapper (module comment). */
export async function listOwnKeys(context: RequestsContext, userId: string, identity: Identity): Promise<OwnKeys> {
  const body = await context.client.request('GET', `/v1/wraps?scope=cred&scope_id=${encodeURIComponent(userId)}`);
  const wraps = isRecord(body) && Array.isArray(body.wraps) ? body.wraps.filter(isRecord) : [];
  const own = wraps
    .filter((w) => w.scope === CRED && w.scope_id === userId && w.recipient_id === userId)
    .filter((w) => Number.isSafeInteger(w.epoch) && (w.epoch as number) >= 1 && typeof w.wrap === 'string')
    .sort((a, b) => (b.epoch as number) - (a.epoch as number));
  const found = new Map<number, OwnKey>();
  const former = new Map<number, OwnKey>();
  for (const wrap of own) {
    const epoch = wrap.epoch as number;
    if (found.has(epoch)) continue;
    const slot = { scope: CRED, scope_id: userId, epoch, recipient: userId };
    try {
      const trusted = selfWrapperKey(context.store, userId, identity, wrap.wrapped_by);
      const wrapper = trusted ?? formerAgentKey(context.store, userId, wrap.wrapped_by) ?? refuse('author_not_writer');
      await verifyWrap(wrapper, slot, wrap.wrap as string, wrap.sig);
      const key = {
        epoch,
        key: await unwrapKey(identity.kem, slot, wrap.wrap as string),
        createdAt: createdAt(wrap.created_at),
      };
      (trusted === null ? former : found).set(epoch, key);
    } catch {
      // a wrap that does not verify or open is skipped (another one of the epoch may)
    }
  }
  const keys = [...found.values()];
  const opening = [...keys, ...[...former.values()].filter((key) => !found.has(key.epoch))].sort(
    (a, b) => b.epoch - a.epoch,
  );
  return { keys, opening, listed: (own[0]?.epoch as number | undefined) ?? 0 };
}

/** The user's own `CK`s the browser builds on, newest epoch first (`listOwnKeys`). */
export async function ownKeys(context: RequestsContext, userId: string, identity: Identity): Promise<OwnKey[]> {
  return (await listOwnKeys(context, userId, identity)).keys;
}

/** The newest own `CK`, or `epoch_stale` when none opens. */
export async function currentKey(context: RequestsContext, userId: string, identity: Identity): Promise<OwnKey> {
  return (await ownKeys(context, userId, identity))[0] ?? refuse('epoch_stale');
}

// --- records ---

export interface SealInput {
  readonly id: string;
  readonly owner: string;
  readonly kind: CredentialKind;
  readonly version: number;
  readonly epoch: number;
  readonly author: string;
  readonly ts: string;
  readonly key: Secret;
  readonly body: unknown;
  readonly signer: CryptoKey;
  /** Fixed salt and nonce (test vectors only). */
  readonly saltNonce?: readonly [Uint8Array, Uint8Array];
}

/** A credential record (module comment); `params_invalid` for a body the kind does not allow. */
export function sealRecord(input: SealInput): Promise<Sealed> {
  if (!validBody(input.kind, input.body)) refuse('params_invalid');
  const header = newHeader({
    id: input.id,
    owner: input.owner,
    kind: input.kind,
    version: input.version,
    epoch: input.epoch,
    author: input.author,
    ts: input.ts,
  });
  return sealCredential(input.key, header, bodyJson(input.body), input.signer, input.saltNonce);
}

/** The signing key of a record's author, or null when the author may not have written it. */
export type AuthorKeys = (author: unknown, header: Fields) => Uint8Array | null;

export interface Opened {
  readonly header: Fields;
  readonly body: unknown;
  readonly summary: Summary;
}

/** Verifies and opens a record of `kind` (module comment); `keys` are the user's own `CK`s by epoch. */
export async function openRecord(
  context: RequestsContext,
  kind: CredentialKind,
  record: unknown,
  keys: readonly OwnKey[],
  authors?: AuthorKeys,
): Promise<Opened> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const header = isRecord(record) && isRecord(record.header) ? (record.header as Fields) : {};
  if (header.owner !== userId || header.kind !== kind) refuse('header_invalid');
  const resolve: AuthorKeys = authors ?? ((author) => authorKey(context.store, userId, identity, author));
  const sigKey = resolve(header.author, header) ?? refuse('author_not_writer');
  const checked = await verifyCredential(record, sigKey);
  const key = keys.find((item) => item.epoch === checked.epoch)?.key ?? refuse('epoch_stale');
  const body = parseJson(await openCredential(key, record, sigKey));
  if (!validBody(kind, body)) refuse('params_invalid');
  return { header: checked, body, summary: summarize(kind, body) };
}

/** An item of `GET /v1/me/credentials`. */
export interface ListedCredential {
  readonly kind: CredentialKind;
  readonly version: number;
  readonly epoch: number;
  readonly updatedAt: string;
}

const isListed = (
  value: unknown,
): value is { kind: CredentialKind; version: number; epoch: number; updated_at: string } =>
  isRecord(value) &&
  isCredentialKind(value.kind) &&
  Number.isSafeInteger(value.version) &&
  (value.version as number) >= 1 &&
  Number.isSafeInteger(value.epoch) &&
  (value.epoch as number) >= 1 &&
  typeof value.updated_at === 'string';

/** `GET /v1/me/credentials`, sorted by kind; a malformed answer is `unavailable`. */
export async function listCredentials(context: RequestsContext): Promise<ListedCredential[]> {
  const body = await context.client.request('GET', '/v1/me/credentials');
  if (!isRecord(body) || !Array.isArray(body.credentials) || !body.credentials.every(isListed)) {
    throw new HubError('unavailable', 200);
  }
  return body.credentials
    .map((item) => ({ kind: item.kind, version: item.version, epoch: item.epoch, updatedAt: item.updated_at }))
    .sort((a, b) => CREDENTIAL_KINDS.indexOf(a.kind) - CREDENTIAL_KINDS.indexOf(b.kind));
}

/** `GET /v1/me/credentials/{kind}`: the stored record, or null when there is none. */
export async function fetchRecord(context: RequestsContext, kind: CredentialKind): Promise<unknown> {
  try {
    const body = await context.client.request('GET', `/v1/me/credentials/${kind}`);
    return isRecord(body) ? (body.record ?? null) : null;
  } catch (error) {
    if (error instanceof HubError && error.code === 'not_found') return null;
    throw error;
  }
}

/** The code of a record that could not be opened (crypto and refusals keep theirs). */
const openCode = (error: unknown): string =>
  error instanceof CryptoError || error instanceof RequestError ? error.code : 'unavailable';

/** Loads, opens and summarizes every record into the `credentials` slice (module comment). */
export async function loadCredentials(context: RequestsContext): Promise<readonly CredentialEntry[]> {
  const generation = context.store.generation;
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const listed = await listCredentials(context);
  const keys = listed.length === 0 ? [] : (await listOwnKeys(context, userId, identity)).opening;
  const entries: CredentialEntry[] = [];
  for (const item of listed) {
    const record = await fetchRecord(context, item.kind);
    if (record === null) continue;
    try {
      const opened = await openRecord(context, item.kind, record, keys);
      entries.push({ ...item, status: 'verified', summary: opened.summary, code: null });
    } catch (error) {
      entries.push({ ...item, status: 'unverifiable', summary: null, code: openCode(error) });
    }
  }
  if (context.store.generation === generation) setCredentials(context.store, entries);
  return entries;
}

async function putOnce(context: RequestsContext, kind: CredentialKind, body: unknown): Promise<number> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const listed = (await listCredentials(context)).find((item) => item.kind === kind);
  const stored = listed === undefined ? null : await fetchRecord(context, kind);
  const storedId = isRecord(stored) && isRecord(stored.header) ? stored.header.id : undefined;
  const expected = stored === null ? 0 : listed!.version;
  const key = await currentKey(context, userId, identity);
  const record = await sealRecord({
    id: typeof storedId === 'string' ? storedId : uuid7(context.now()),
    owner: userId,
    kind,
    version: expected + 1,
    epoch: key.epoch,
    author: userId,
    ts: formatTime(context.now()),
    key: key.key,
    body,
    signer: identity.sig.privateKey,
  });
  const answer = await context.client.request('PUT', `/v1/me/credentials/${kind}`, {
    body: { record, expected_version: expected },
  });
  if (!isRecord(answer) || answer.version !== expected + 1) throw new HubError('unavailable', 200);
  return expected + 1;
}

/** Stores `body` as the user's `kind` credential (module comment); returns the new version. */
export async function putCredential(context: RequestsContext, kind: CredentialKind, body: unknown): Promise<number> {
  if (!validBody(kind, body)) refuse('params_invalid');
  try {
    return await putOnce(context, kind, body);
  } catch (error) {
    if (!(error instanceof HubError) || (error.code !== 'version_conflict' && error.code !== 'epoch_stale'))
      throw error;
    return putOnce(context, kind, body);
  }
}

/** `DELETE /v1/me/credentials/{kind}` (`404` counts as done). */
export async function deleteCredential(context: RequestsContext, kind: CredentialKind): Promise<void> {
  try {
    await context.client.request('DELETE', `/v1/me/credentials/${kind}`);
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'not_found') throw error;
  }
}
