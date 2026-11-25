// Claude subscriptions logged in centrally (KOR-69, ADR-090, rules E-30 and E-31; 03-api.md section 4.8; twin of
// `agent_platform.subscriptions`).
//
// - `loadSubscriptions`: `GET /v1/me/subscriptions` (metadata and the newest reading), then every record (`GET
//   /v1/subscriptions/{id}`), checked like every own record: the answer names the user, the header the record, the
//   user and the kind; the author is the user or a device the verified logs entitle to the user's `CK`; signature, the
//   user's `CK` of the record's epoch, a body of `contracts/credentials/claude_subscription.json` (`isSubscriptionRecord`).
//   Only label, note, address, status, use and the reading leave this module – never the token. The primary record is
//   `own sessions`; a record that does not open is `unverifiable` with its code. Most room first, unknown last.
// - `subscriptionDevices`: the devices that may run the login of a further subscription – those of `connectDevices`
//   (a fresh organization gets a device first) that announced `subscriptions`; the online one first.
// - `startSubscriptionLogin`: `claude.login` with `subscription_id` (a new UUIDv7 for `newSubscriptionId`), `name`,
//   `note` and, automatic only, `email`; the rest of the flow is the relay of `login.ts` (`sendCode`, `loginProgress`).
// - `changeSubscription`: an own further subscription opened, changed and sealed again under the newest own `CK`
//   (version + 1, author the user), `PUT /v1/subscriptions/{id}` with the mirror of its sealed use; one retry after a
//   concurrent write. `renameSubscription`, `noteSubscription`, `logoutSubscription` (drops the token) and
//   `setSubscriptionUse` (`null` = measure only) build on it. The primary record is never changed here
//   (`invalid_argument`).
// - `removeSubscription`: `DELETE /v1/subscriptions/{id}` (`404` counts as done).
import { uuid7 } from '../encoding/ids';
import { bodyJson, parseJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, openCredential, sealCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import { type OwnKey, ownKeys } from './records';
import { authorKey, connectDevices, type DeviceChoice } from '../github/connection';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { EMAIL_RE, isLabel } from '../requests/bodies';
import {
  eligibleDevices,
  type PreparedRequest,
  prepareRequest,
  RequestError,
  type RequestsContext,
  submitRequest,
} from '../requests/client';
import type { TasksContext } from '../tasks/pull';
import type { LoginMode } from './login';

export const KIND = 'claude_subscription';
export const FEATURE = 'subscriptions';
export const MAX_ORGS = 50;
/** Where the user revokes a token at the provider (the platform cannot). */
export const REVOKE_URL = 'https://claude.ai/settings';
const CONCURRENT = new Set(['version_conflict', 'epoch_stale']);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TOKEN_RE = /^[!-~]{1,4096}$/;
const BODY_KEYS = ['token', 'email', 'kind', 'expires_at', 'name', 'note', 'use'];

export interface SubscriptionUse {
  readonly pool: boolean;
  readonly orgs: readonly string[];
}

export type SubscriptionStatus = 'logged_in' | 'not_logged_in' | 'not_readable' | 'unverifiable';

export interface SubscriptionReading {
  readonly measuredAt: string;
  readonly deviceId: string;
  readonly unreadable: boolean;
  readonly weekPercent: number | null;
  readonly weekResetsAt: string | null;
  readonly windowPercent: number | null;
  readonly windowResetsAt: string | null;
}

export interface Subscription {
  readonly id: string;
  readonly primary: boolean;
  readonly status: SubscriptionStatus;
  /** The crypto or form code of an `unverifiable` record. */
  readonly code: string | null;
  readonly name: string | null;
  readonly note: string | null;
  readonly email: string | null;
  /** `null` for the primary record (own sessions) and for an unverifiable one. */
  readonly use: SubscriptionUse | null;
  readonly reading: SubscriptionReading | null;
}

function refuse(code: string): never {
  throw new RequestError(code);
}

export const isUse = (value: unknown): value is SubscriptionUse =>
  isRecord(value) &&
  Object.keys(value).length === 2 &&
  typeof value.pool === 'boolean' &&
  Array.isArray(value.orgs) &&
  value.orgs.length <= MAX_ORGS &&
  value.orgs.every((org) => typeof org === 'string' && UUID_RE.test(org)) &&
  new Set(value.orgs).size === value.orgs.length;

/** A body of `contracts/credentials/claude_subscription.json` (also of a further subscription). */
export function isSubscriptionRecord(value: unknown): value is Record<string, unknown> {
  if (!isRecord(value) || !('email' in value) || !('kind' in value)) return false;
  if (!Object.keys(value).every((key) => BODY_KEYS.includes(key))) return false;
  const email = value.email;
  return (
    (value.kind === 'setup_token' || value.kind === 'login_token') &&
    (email === null || (typeof email === 'string' && email.length <= 254 && EMAIL_RE.test(email))) &&
    (value.token === undefined || (typeof value.token === 'string' && TOKEN_RE.test(value.token))) &&
    (value.expires_at === undefined || typeof value.expires_at === 'string') &&
    (value.name === undefined || isLabel(value.name, 64)) &&
    (value.note === undefined || isLabel(value.note, 500)) &&
    (value.use === undefined || isUse(value.use))
  );
}

// --- the list ---

interface Meta {
  readonly id: string;
  readonly primary: boolean;
  readonly reading: unknown;
}

const isMeta = (value: unknown): boolean =>
  isRecord(value) &&
  typeof value.id === 'string' &&
  typeof value.primary === 'boolean' &&
  Number.isSafeInteger(value.version) &&
  (value.reading === null || isRecord(value.reading));

const numberOr = (value: unknown): number | null => (typeof value === 'number' ? value : null);
const textOr = (value: unknown): string | null => (typeof value === 'string' ? value : null);

/** The reading as the overview shows it: the weekly and the five-hour window (null before the first reading). */
export function readingOf(value: unknown): SubscriptionReading | null {
  if (!isRecord(value) || typeof value.fetched_at !== 'string' || typeof value.device_id !== 'string') return null;
  const windows = Array.isArray(value.windows) ? value.windows.filter(isRecord) : [];
  const week = windows.find((item) => item.window === 'seven_day') ?? {};
  const window = windows.find((item) => item.window === 'five_hour') ?? {};
  return {
    measuredAt: value.fetched_at,
    deviceId: value.device_id,
    unreadable: value.unreadable === true,
    weekPercent: numberOr(week.used_percent),
    weekResetsAt: textOr(week.resets_at),
    windowPercent: numberOr(window.used_percent),
    windowResetsAt: textOr(window.resets_at),
  };
}

/** `GET /v1/me/subscriptions`; a malformed answer is `unavailable`. */
export async function listSubscriptions(context: RequestsContext): Promise<Meta[]> {
  const body = await context.client.request('GET', '/v1/me/subscriptions');
  if (!isRecord(body) || !Array.isArray(body.subscriptions) || !body.subscriptions.every(isMeta)) {
    throw new HubError('unavailable', 200);
  }
  return body.subscriptions as Meta[];
}

/** `GET /v1/subscriptions/{id}`, or null when the record is gone. */
export async function fetchSubscription(context: RequestsContext, id: string): Promise<unknown> {
  try {
    return await context.client.request('GET', `/v1/subscriptions/${encodeURIComponent(id)}`);
  } catch (error) {
    if (error instanceof HubError && error.code === 'not_found') return null;
    throw error;
  }
}

export interface OpenedSubscription {
  readonly header: Fields;
  readonly primary: boolean;
  readonly body: Record<string, unknown>;
}

/** Verifies and opens the hub's answer for the own subscription `id` (module comment). */
export async function openSubscription(
  context: RequestsContext,
  id: string,
  answer: unknown,
  keys: readonly OwnKey[],
): Promise<OpenedSubscription> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const found = isRecord(answer) ? answer : {};
  const record = found.record;
  const header = isRecord(record) && isRecord(record.header) ? (record.header as Fields) : {};
  if (found.owner_id !== userId || header.id !== id || header.owner !== userId || header.kind !== KIND)
    refuse('header_invalid');
  const sigKey = authorKey(context.store, userId, identity, header.author) ?? refuse('author_not_writer');
  const checked = await verifyCredential(record, sigKey);
  const secret = keys.find((item) => item.epoch === checked.epoch)?.key ?? refuse('epoch_stale');
  const body = parseJson(await openCredential(secret, record, sigKey));
  if (!isSubscriptionRecord(body)) refuse('params_invalid');
  return { header: checked, primary: found.primary === true, body };
}

const errorCode = (error: unknown): string =>
  error instanceof RequestError || error instanceof HubError ? error.code : 'unverifiable';

async function entryOf(context: RequestsContext, meta: Meta, keys: readonly OwnKey[]): Promise<Subscription | null> {
  const answer = await fetchSubscription(context, meta.id);
  if (answer === null) return null;
  const reading = readingOf(meta.reading);
  const base = { id: meta.id, primary: meta.primary, reading };
  let opened: OpenedSubscription;
  try {
    opened = await openSubscription(context, meta.id, answer, keys);
  } catch (error) {
    const blank = { name: null, note: null, email: null, use: null };
    return { ...base, ...blank, status: 'unverifiable', code: errorCode(error) };
  }
  const { body } = opened;
  let status: SubscriptionStatus = 'logged_in';
  if (body.token === undefined) status = 'not_logged_in';
  else if (reading?.unreadable === true) status = 'not_readable';
  const use = isUse(body.use) ? body.use : { pool: false, orgs: [] };
  return {
    ...base,
    status,
    code: null,
    name: textOr(body.name),
    note: textOr(body.note),
    email: textOr(body.email),
    use: meta.primary ? null : use,
  };
}

/** Every own subscription (module comment), most room first. */
export async function loadSubscriptions(context: RequestsContext): Promise<Subscription[]> {
  const listed = await listSubscriptions(context);
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const keys = listed.length === 0 ? [] : await ownKeys(context, userId, identity);
  const found: Subscription[] = [];
  for (const meta of listed) {
    const entry = await entryOf(context, meta, keys);
    if (entry !== null) found.push(entry);
  }
  const room = (item: Subscription) => item.reading?.weekPercent ?? Number.POSITIVE_INFINITY;
  return found.sort((a, b) => room(a) - room(b) || Number(a.id > b.id) - Number(a.id < b.id));
}

// --- the login on a device ---

/** The devices that may log a further subscription in (module comment) and the preselected one. */
export async function subscriptionDevices(
  context: TasksContext,
): Promise<{ readonly devices: readonly DeviceChoice[]; readonly preselected: string | null }> {
  const found = await connectDevices(context);
  const able = new Set(
    (await eligibleDevices(context))
      .filter((item) => (item.features ?? []).includes(FEATURE))
      .map((item) => item.device_id),
  );
  const devices = found.devices.filter((item) => able.has(item.device));
  return { devices, preselected: devices.find((item) => item.online)?.device ?? null };
}

export const newSubscriptionId = (): string => uuid7();

export interface LoginInput {
  readonly subscriptionId: string;
  readonly mode: LoginMode;
  readonly name?: string;
  readonly note?: string;
  readonly email?: string;
}

/** Seals and posts `claude.login` for a further subscription to `device`. */
export async function startSubscriptionLogin(
  context: RequestsContext,
  device: string,
  input: LoginInput,
): Promise<PreparedRequest> {
  const body: Record<string, unknown> = { mode: input.mode, subscription_id: input.subscriptionId };
  for (const field of ['name', 'note', 'email'] as const) {
    const value = input[field]?.trim();
    if (value !== undefined && value !== '') body[field] = value;
  }
  const prepared = await prepareRequest(context, { device, kind: 'claude.login', body });
  await submitRequest(context, prepared);
  return prepared;
}

// --- changes by the owner ---

async function changeOnce(
  context: RequestsContext,
  id: string,
  edit: (body: Record<string, unknown>) => void,
): Promise<boolean> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const answer = (await fetchSubscription(context, id)) ?? refuse('not_found');
  const keys = await ownKeys(context, userId, identity);
  const opened = await openSubscription(context, id, answer, keys);
  if (opened.primary) refuse('invalid_argument');
  const body = { ...opened.body };
  edit(body);
  const latest = keys[0]!; // the key that opened the record is one of them

  const version = opened.header.version as number;
  const header = newHeader({
    id,
    owner: userId,
    kind: KIND,
    version: version + 1,
    epoch: latest.epoch,
    author: userId,
    ts: formatTime(context.now()),
  });
  const record = await sealCredential(latest.key, header, bodyJson(body), identity.sig.privateKey);
  const use = isUse(body.use) ? body.use : { pool: false, orgs: [] };
  try {
    await context.client.request('PUT', `/v1/subscriptions/${encodeURIComponent(id)}`, {
      body: { record, expected_version: version, use },
    });
    return true;
  } catch (error) {
    if (error instanceof HubError && CONCURRENT.has(error.code)) return false;
    throw error;
  }
}

/** Changes an own further subscription (module comment). */
export async function changeSubscription(
  context: RequestsContext,
  id: string,
  edit: (body: Record<string, unknown>) => void,
): Promise<void> {
  if (await changeOnce(context, id, edit)) return;
  if (!(await changeOnce(context, id, edit))) refuse('version_conflict');
}

export function renameSubscription(context: RequestsContext, id: string, name: string): Promise<void> {
  if (!isLabel(name.trim(), 64)) refuse('invalid_argument');
  return changeSubscription(context, id, (body) => {
    body.name = name.trim();
  });
}

/** Sets the note, or removes it for an empty text. */
export function noteSubscription(context: RequestsContext, id: string, note: string): Promise<void> {
  const text = note.trim();
  if (text !== '' && !isLabel(text, 500)) refuse('invalid_argument');
  return changeSubscription(context, id, (body) => {
    delete body.note;
    if (text !== '') body.note = text;
  });
}

export function logoutSubscription(context: RequestsContext, id: string): Promise<void> {
  return changeSubscription(context, id, (body) => {
    delete body.token;
  });
}

/** The use of a further subscription; `null` or nothing allowed = measure only. */
export function setSubscriptionUse(context: RequestsContext, id: string, use: SubscriptionUse | null): Promise<void> {
  const orgs = [...new Set(use?.orgs ?? [])].sort();
  const wanted = { pool: use?.pool ?? false, orgs };
  if (!isUse(wanted)) refuse('invalid_argument');
  return changeSubscription(context, id, (body) => {
    delete body.use;
    if (wanted.pool || wanted.orgs.length > 0) body.use = wanted;
  });
}

export async function removeSubscription(context: RequestsContext, id: string): Promise<void> {
  try {
    await context.client.request('DELETE', `/v1/subscriptions/${encodeURIComponent(id)}`);
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'not_found') throw error;
  }
}
