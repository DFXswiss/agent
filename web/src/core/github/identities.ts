// GitHub identities (ADR-078, key model KM-35, 03-api.md section 4.8, 06-web.md section 8.1): the `github` credential
// records of the user – own accounts, verified and opened in the browser – and the company accounts of the user's
// organizations, which no browser can open (they are sealed to the organization's devices only) and are shown by
// metadata.
//
// - `loadIdentities`: `GET /v1/me/github-identities` (metadata and the defaults of organizations and workgroups), then
//   every own record (`GET /v1/github-identities/{id}`), checked by `openIdentity`: the header names the record
//   (`id`), the user as owner and the kind `github`; the author is the user or a device the verified logs entitle to
//   the user's `CK` (`authorKey`); it opens with the user's `CK` of the record's epoch (own self-wrap); the body is
//   exactly what `contracts/credentials/github.json` allows. Only the login, display name, source, scopes, the GitHub
//   account ID, whether the record holds a signing key, the connection time and whether the token is one the platform
//   minted leave this module – never the token or the key seed. A record that does not verify is kept as
//   `unverifiable` with its code. Company records stay `company` (organization, times; the login only when this
//   browser stored the record in this sign-in, `knownLogins`). The result goes to the `githubIdentities` slice; the
//   single connection of before (`connection.ts`) stays the primary record.
// - `makePrimary`, `removeIdentity`, `setGithubDefault`, `setTaskIdentity`: `PUT /v1/github-identities/{id}/primary`,
//   `DELETE /v1/github-identities/{id}` (`404` counts as done), `PUT /v1/github-defaults/{scope}/{scope_id}` and
//   `PUT /v1/tasks/{task}/github-identity` (the task row in the store follows); `outdatedDevices` reads the devices of a
//   `409 peer_outdated` (`detail.devices`, when the hub names them).
// - `storeCompanyIdentity`: the final answer of a company account's `github.connect` carries the credential body for
//   the requesting admin (`companyBody`). The admin's client writes it to the record being reconnected (same ID,
//   version + 1) or to a new UUIDv7 (version 1), under a fresh key of a new epoch of the organization's `cred` scope:
//   the epoch after the newest one the hub lists for the organization's records, the key wrapped (and the wrap
//   signed) for every reader device – the active devices of the organization assigned to one of its workgroups, keys
//   from the verified log, decertified platform devices left out – the body sealed under it (header owner = the
//   organization, author = the admin), all in one `PUT /v1/github-identities/{id}` with `key {epoch, wraps}`. The key
//   is dropped afterwards: the admin cannot read the record back. One retry after `version_conflict` or `epoch_stale`
//   (another admin wrote meanwhile) with a fresh list. The hub answers an epoch that is not the scope's next one with
//   `409 version_conflict` and `detail.expected` = the epoch it expects (after the record with the newest epoch was
//   deleted the scope's epoch is higher than any listed record's); a version conflict of the record answers the same
//   code. So when the record's version is unchanged in the fresh list, the retry takes `detail.expected` as the epoch
//   (never lower than the listed one; as the agent tool, `agent_platform.identities`).
// - `rewrapOwnIdentities` (job `github-rewrap`, and after a `CK` rotation): own non-primary records whose epoch is
//   older than the newest own `CK` are re-sealed under it (same ID, version + 1, author the user). Records the browser
//   cannot open are left alone; a concurrent write (`version_conflict`, `epoch_stale`) waits for the next run.
// - `usableIdentities` / `identityLogin`: the identities a task of an organization may choose (own accounts and the
//   company accounts of that organization) and the login of an identity for the Operations tab.
import { uuid7 } from '../encoding/ids';
import { parseJson } from '../encoding/json';
import { formatTime, parseTime } from '../encoding/time';
import { CryptoError } from '../crypto/errors';
import { openCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import { Secret } from '../crypto/secret';
import { type OwnKey, ownKeys, sealRecord } from '../credentials/records';
import { buildWraps } from '../log/catchup';
import { excludedFor } from '../log/platform';
import { ORG, type OrgState, USER } from '../log/state';
import { compareSlots, CRED, credReaders } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { verifiedPlatform } from '../org/batch';
import { isRecord } from '../registry';
import { type GithubBody, isGithubBody, type Source } from '../requests/bodies';
import { RequestError, type RequestView, type RequestsContext } from '../requests/client';
import { noteCompanyLogin, setGithubIdentities } from '../store/slices/githubIdentities';
import { orgEntry } from '../store/slices/orgs';
import { putTasks } from '../store/slices/tasks';
import { authorKey, KIND, REVOCABLE_PREFIX } from './connection';

export type OwnerKind = 'user' | 'org';
export type DefaultScope = 'org' | 'wg';

/** What the hub lists of an identity (`github_identity_meta`). */
export interface IdentityMeta {
  readonly id: string;
  readonly ownerId: string;
  readonly ownerKind: OwnerKind;
  readonly primary: boolean;
  readonly version: number;
  readonly epoch: number;
  readonly createdAt: string;
  readonly updatedAt: string;
}

/** What may be shown of an opened own identity (never the token or the key seed). */
export interface IdentityAccount {
  readonly login: string;
  readonly name: string | null;
  readonly source: Source;
  readonly scopes: readonly string[];
  /** The GitHub account ID (`user_id`). */
  readonly githubUserId: number;
  readonly signingKey: boolean;
  /** When the connection was made (the body's `created_at`). */
  readonly connectedAt: string;
  /** A GitHub App user token the devices revoke at GitHub on disconnect. */
  readonly revocable: boolean;
}

export type VerifiedIdentity = IdentityMeta & { readonly status: 'verified'; readonly account: IdentityAccount };
/** A company account: metadata only (no browser can open it, KM-35). */
export type CompanyIdentity = IdentityMeta & { readonly status: 'company' };

export type GithubIdentity =
  VerifiedIdentity | CompanyIdentity | (IdentityMeta & { readonly status: 'unverifiable'; readonly code: string });

/** The default identity of an organization or workgroup (`github_default`). */
export interface GithubDefault {
  readonly scope: DefaultScope;
  readonly scopeId: string;
  readonly identityId: string;
  readonly setBy: string;
  readonly setAt: string;
}

export interface IdentityList {
  readonly identities: readonly IdentityMeta[];
  readonly defaults: readonly GithubDefault[];
}

function refuse(code: string): never {
  throw new RequestError(code);
}

const isText = (value: unknown): value is string => typeof value === 'string';
const isCount = (value: unknown, min: number): value is number =>
  Number.isSafeInteger(value) && (value as number) >= min;

const isMeta = (value: unknown): boolean =>
  isRecord(value) &&
  isText(value.id) &&
  isText(value.owner_id) &&
  (value.owner_kind === USER || value.owner_kind === ORG) &&
  typeof value.primary === 'boolean' &&
  isCount(value.version, 1) &&
  isCount(value.epoch, 1) &&
  isText(value.created_at) &&
  isText(value.updated_at);

const isDefault = (value: unknown): boolean =>
  isRecord(value) &&
  (value.scope === 'org' || value.scope === 'wg') &&
  isText(value.scope_id) &&
  isText(value.identity_id) &&
  isText(value.set_by) &&
  isText(value.set_at);

/** `GET /v1/me/github-identities`; a malformed answer is `unavailable`. */
export async function listIdentities(context: RequestsContext): Promise<IdentityList> {
  const body = await context.client.request('GET', '/v1/me/github-identities');
  if (
    !isRecord(body) ||
    !Array.isArray(body.identities) ||
    !body.identities.every(isMeta) ||
    !Array.isArray(body.defaults) ||
    !body.defaults.every(isDefault)
  ) {
    throw new HubError('unavailable', 200);
  }
  return {
    identities: (body.identities as Record<string, unknown>[]).map((item) => ({
      id: item.id as string,
      ownerId: item.owner_id as string,
      ownerKind: item.owner_kind as OwnerKind,
      primary: item.primary as boolean,
      version: item.version as number,
      epoch: item.epoch as number,
      createdAt: item.created_at as string,
      updatedAt: item.updated_at as string,
    })),
    defaults: (body.defaults as Record<string, unknown>[]).map((item) => ({
      scope: item.scope as DefaultScope,
      scopeId: item.scope_id as string,
      identityId: item.identity_id as string,
      setBy: item.set_by as string,
      setAt: item.set_at as string,
    })),
  };
}

/** `GET /v1/github-identities/{id}`: the hub's answer, or null when the record is gone. */
export async function fetchIdentity(context: RequestsContext, id: string): Promise<unknown> {
  try {
    return await context.client.request('GET', `/v1/github-identities/${encodeURIComponent(id)}`);
  } catch (error) {
    if (error instanceof HubError && error.code === 'not_found') return null;
    throw error;
  }
}

export interface OpenedIdentity {
  readonly header: Fields;
  readonly body: GithubBody;
}

/** Verifies and opens the hub's answer for the own identity `id` (module comment); `keys` are the user's `CK`s. */
export async function openIdentity(
  context: RequestsContext,
  id: string,
  answer: unknown,
  keys: readonly OwnKey[],
): Promise<OpenedIdentity> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const found = isRecord(answer) ? answer : {};
  const record = found.record;
  const header = isRecord(record) && isRecord(record.header) ? (record.header as Fields) : {};
  if (
    found.owner_id !== userId ||
    found.owner_kind !== USER ||
    header.id !== id ||
    header.owner !== userId ||
    header.kind !== KIND
  )
    refuse('header_invalid');
  const sigKey = authorKey(context.store, userId, identity, header.author) ?? refuse('author_not_writer');
  const checked = await verifyCredential(record, sigKey);
  const secret = keys.find((item) => item.epoch === checked.epoch)?.key ?? refuse('epoch_stale');
  const body = parseJson(await openCredential(secret, record, sigKey));
  if (!isGithubBody(body)) refuse('params_invalid');
  parseTime(body.created_at);
  return { header: checked, body };
}

/** The part of a checked body that may be shown. */
export function accountOf(body: GithubBody): IdentityAccount {
  return {
    login: body.login,
    name: body.name ?? null,
    source: body.source ?? 'device_flow',
    scopes: [...body.scopes],
    githubUserId: body.user_id,
    signingKey: body.signing_key !== undefined,
    connectedAt: body.created_at,
    revocable: body.token.startsWith(REVOCABLE_PREFIX),
  };
}

/** The code of a record that could not be opened (crypto and refusals keep theirs). */
const openCode = (error: unknown): string =>
  error instanceof CryptoError || error instanceof RequestError ? error.code : 'unavailable';

/** Loads, verifies and stores every identity of the user (module comment). */
export async function loadIdentities(context: RequestsContext): Promise<readonly GithubIdentity[]> {
  const generation = context.store.generation;
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const list = await listIdentities(context);
  const own = list.identities.filter((item) => item.ownerKind === USER);
  const keys = own.length > 0 ? await ownKeys(context, userId, identity) : [];
  const identities: GithubIdentity[] = [];
  for (const meta of list.identities) {
    if (meta.ownerKind === ORG) {
      identities.push({ ...meta, status: 'company' });
      continue;
    }
    const answer = await fetchIdentity(context, meta.id);
    if (answer === null) continue;
    try {
      const opened = await openIdentity(context, meta.id, answer, keys);
      identities.push({ ...meta, status: 'verified', account: accountOf(opened.body) });
    } catch (error) {
      identities.push({ ...meta, status: 'unverifiable', code: openCode(error) });
    }
  }
  if (context.store.generation === generation) setGithubIdentities(context.store, identities, list.defaults);
  return identities;
}

// --- changes ---

/** `PUT /v1/github-identities/{id}/primary`: an own record becomes the primary one. */
export async function makePrimary(context: RequestsContext, id: string): Promise<void> {
  await context.client.request('PUT', `/v1/github-identities/${encodeURIComponent(id)}/primary`, { body: {} });
}

/** `DELETE /v1/github-identities/{id}` (`404` counts as done). */
export async function removeIdentity(context: RequestsContext, id: string): Promise<void> {
  try {
    await context.client.request('DELETE', `/v1/github-identities/${encodeURIComponent(id)}`);
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'not_found') throw error;
  }
}

/** `PUT /v1/github-defaults/{scope}/{scope_id}` with `identityId` (null clears the default). */
export async function setGithubDefault(
  context: RequestsContext,
  scope: DefaultScope,
  scopeId: string,
  identityId: string | null,
): Promise<void> {
  await context.client.request('PUT', `/v1/github-defaults/${scope}/${encodeURIComponent(scopeId)}`, {
    body: { identity_id: identityId },
  });
}

/** `PUT /v1/tasks/{task}/github-identity` (null clears the choice); the task row in the store follows. */
export async function setTaskIdentity(
  context: RequestsContext,
  task: string,
  identityId: string | null,
): Promise<void> {
  const generation = context.store.generation;
  await context.client.request('PUT', `/v1/tasks/${encodeURIComponent(task)}/github-identity`, {
    body: { identity_id: identityId },
  });
  const row = context.store.getState().tasks.tasks[task];
  if (row !== undefined && context.store.generation === generation) {
    putTasks(context.store, [{ ...row, github_identity: identityId }]);
  }
}

/** The devices a `409 peer_outdated` names (`detail.devices`), or null for another error or without names. */
export function outdatedDevices(error: unknown): readonly string[] | null {
  if (!(error instanceof HubError) || error.code !== 'peer_outdated' || !isRecord(error.detail)) return null;
  const devices = error.detail.devices;
  return Array.isArray(devices) && devices.length > 0 && devices.every(isText) ? devices : null;
}

// --- company accounts ---

/** The credential body of the final answer of a company account's `github.connect`, or null. */
export function companyBody(view: RequestView): GithubBody | null {
  const last = view.responses.filter((item) => item.status === 'verified').at(-1);
  const body = last?.body;
  return body?.step === 'done' && body.body !== undefined ? body.body : null;
}

/** `PUT /v1/github-identities/{id}`; checks the stored version. */
async function putIdentity(
  context: RequestsContext,
  id: string,
  body: Record<string, unknown>,
  expected: number,
): Promise<number> {
  const answer = await context.client.request('PUT', `/v1/github-identities/${encodeURIComponent(id)}`, {
    body: { ...body, expected_version: expected },
  });
  if (!isRecord(answer) || answer.version !== expected + 1) throw new HubError('unavailable', 200);
  return expected + 1;
}

/** The devices that read the company records of `org` (module comment), sorted; keys from the verified log. */
export function companyReaders(context: RequestsContext, state: OrgState): string[] {
  const excluded = excludedFor(verifiedPlatform(context.store), state);
  return [...credReaders([state], state.org, excluded)].sort();
}

/** Codes of a concurrent write (another browser, device or admin wrote in between). */
const CONCURRENT: ReadonlySet<string> = new Set(['version_conflict', 'epoch_stale']);

/** The highest epoch the contracts allow (`common.json` `epoch`). */
const MAX_EPOCH = 2 ** 31 - 1;

/** A concurrent write of a company record: the version that was sent and the hub's `detail.expected`. */
class Conflict extends Error {
  constructor(
    readonly version: number,
    readonly expected: number | null,
    readonly error: HubError,
  ) {
    super('conflict');
  }
}

/** `detail.expected` of a conflict when it is an epoch, else null. */
function expectedEpoch(error: HubError): number | null {
  const value = isRecord(error.detail) ? error.detail.expected : undefined;
  return Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= MAX_EPOCH
    ? (value as number)
    : null;
}

async function storeCompanyOnce(
  context: RequestsContext,
  org: string,
  body: GithubBody,
  reconnect: string | null,
  previous: Conflict | null,
): Promise<{ readonly id: string; readonly version: number }> {
  const { userId, identity } = context.store.getState().me;
  const log = orgEntry(context.store.getState().orgs, org).log;
  if (userId === null || identity === null) refuse('unauthorized');
  if (log === null || !log.state.isAdmin(userId)) refuse('forbidden');
  const readers = companyReaders(context, log.state);
  if (readers.length === 0) refuse('no_device_available');
  const list = await listIdentities(context);
  const records = list.identities.filter((item) => item.ownerKind === ORG && item.ownerId === org);
  const known = reconnect === null ? undefined : records.find((item) => item.id === reconnect);
  if (reconnect !== null && known === undefined) refuse('not_found');
  const id = known?.id ?? uuid7(context.now());
  const expected = known?.version ?? 0;
  let epoch = Math.max(0, ...records.map((item) => item.epoch)) + 1;
  // the version was right: the hub named the epoch it expects (module comment)
  if (previous?.version === expected && previous.expected !== null) epoch = Math.max(epoch, previous.expected);
  const key = Secret.random(32);
  const slots = readers.map((recipient) => ({ scope: CRED, scope_id: org, epoch, recipient })).sort(compareSlots);
  const wraps = await buildWraps(identity, slots, () => key, [log.state]);
  const record = await sealRecord({
    id,
    owner: org,
    kind: KIND,
    version: expected + 1,
    epoch,
    author: userId,
    ts: formatTime(context.now()),
    key,
    body,
    signer: identity.sig.privateKey,
  });
  try {
    return { id, version: await putIdentity(context, id, { owner_id: org, record, key: { epoch, wraps } }, expected) };
  } catch (error) {
    if (error instanceof HubError && CONCURRENT.has(error.code))
      throw new Conflict(expected, expectedEpoch(error), error);
    throw error;
  }
}

/**
 * Seals and stores the body of a company account for `org` (module comment) into the record `reconnect` or a new one;
 * returns the record ID and version and keeps the login for this sign-in (`knownLogins`).
 */
export async function storeCompanyIdentity(
  context: RequestsContext,
  org: string,
  body: GithubBody,
  reconnect: string | null = null,
): Promise<{ readonly id: string; readonly version: number }> {
  if (!isGithubBody(body)) refuse('params_invalid');
  const generation = context.store.generation;
  let stored: { readonly id: string; readonly version: number };
  try {
    stored = await storeCompanyOnce(context, org, body, reconnect, null);
  } catch (error) {
    if (!(error instanceof Conflict)) throw error;
    try {
      stored = await storeCompanyOnce(context, org, body, reconnect, error);
    } catch (again) {
      throw again instanceof Conflict ? again.error : again;
    }
  }
  if (context.store.generation === generation) noteCompanyLogin(context.store, stored.id, body.login);
  return stored;
}

// --- re-sealing under the newest own key ---

/** Re-seals one own record under the newest `CK` (same ID, version + 1, author the user); false when skipped. */
async function reseal(context: RequestsContext, meta: IdentityMeta, keys: readonly OwnKey[]): Promise<boolean> {
  const { userId, identity } = context.store.getState().me;
  const latest = keys[0]!;
  const answer = await fetchIdentity(context, meta.id);
  if (answer === null) return false;
  let opened: OpenedIdentity;
  try {
    opened = await openIdentity(context, meta.id, answer, keys);
  } catch {
    return false;
  }
  const version = opened.header.version as number;
  const record = await sealRecord({
    id: meta.id,
    owner: userId!,
    kind: KIND,
    version: version + 1,
    epoch: latest.epoch,
    author: userId!,
    ts: formatTime(context.now()),
    key: latest.key,
    body: opened.body,
    signer: identity!.sig.privateKey,
  });
  try {
    await putIdentity(context, meta.id, { owner_id: userId, record }, version);
    return true;
  } catch (error) {
    if (error instanceof HubError && CONCURRENT.has(error.code)) return false;
    throw error;
  }
}

/** Own non-primary records under an older `CK` epoch, re-sealed under the newest (module comment); returns the count. */
export async function rewrapOwnIdentities(context: RequestsContext): Promise<number> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) return 0;
  const listed = await listIdentities(context);
  const own = listed.identities.filter((item) => item.ownerKind === USER && item.ownerId === userId && !item.primary);
  if (own.length === 0) return 0;
  const keys = await ownKeys(context, userId, identity);
  const latest = keys[0];
  if (latest === undefined) return 0;
  let count = 0;
  for (const meta of own) {
    if (meta.epoch < latest.epoch && (await reseal(context, meta, keys))) count += 1;
  }
  return count;
}

// --- views ---

/** The identities a task of `org` may choose: own verified accounts, then the company accounts of `org`. */
export function usableIdentities(
  identities: readonly GithubIdentity[],
  userId: string | null,
  org: string,
): (VerifiedIdentity | CompanyIdentity)[] {
  return [
    ...identities.filter((item): item is VerifiedIdentity => item.status === 'verified' && item.ownerId === userId),
    ...identities.filter((item): item is CompanyIdentity => item.status === 'company' && item.ownerId === org),
  ];
}

/** How an identity reads in a task (Operations tab, task header). */
export type IdentityName =
  | { readonly kind: 'login'; readonly login: string }
  | { readonly kind: 'company'; readonly org: string; readonly login: string | null }
  | { readonly kind: 'other' };

/** The name of identity `id`: an own login, a company account of an organization (with the login this browser saw
 *  when it stored the record), or another account. */
export function identityName(
  identities: readonly GithubIdentity[],
  knownLogins: Readonly<Record<string, string>>,
  id: string,
): IdentityName {
  const found = identities.find((item) => item.id === id);
  if (found?.status === 'verified') return { kind: 'login', login: found.account.login };
  if (found?.status === 'company') return { kind: 'company', org: found.ownerId, login: knownLogins[id] ?? null };
  return { kind: 'other' };
}
