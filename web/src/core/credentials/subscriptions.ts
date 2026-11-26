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
// - KOR-73 (ADR-098): a further subscription may hold its own `mailbox` (the body of `credentials/mailbox.json`,
//   sealed like the rest; only its address leaves this module). A device that announced `subscriptions.parallel`
//   (`subscriptionDevices().parallel`) gets the new path: `createSubscription` writes the record before its login
//   (no token, kind `oauth_login`, label, note, mailbox and use with its mirror, `expected_version` 0), the login is
//   automatic with the record's own mailbox, may carry the data of a new account (`account`) and is posted with the
//   subscription as its `slot`, so several logins run side by side (`addSubscriptions`: one login per entry, all at
//   once, each with its own outcome). Every login root to such a device says `extended: true` (this client reads
//   `verify`, `reason` and the new stages). `setSubscriptionMailbox` sets or removes the mailbox of an existing one. A
//   login that needs the new path (own mailbox, record first, registration) is refused for an older device
//   (`no_device_available`); otherwise the single login of KOR-69 stays (no record first, no slot).
import { uuid7 } from '../encoding/ids';
import { bodyJson, parseJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, openCredential, sealCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import { currentKey, type OwnKey, ownKeys } from './records';
import { authorKey, connectDevices, type DeviceChoice } from '../github/connection';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { type Consent, EMAIL_RE, isLabel } from '../requests/bodies';
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
/** A device that runs logins side by side, each with the subscription's own mailbox (KOR-73, ADR-098). */
export const PARALLEL_FEATURE = 'subscriptions.parallel';
export const MAX_ORGS = 50;
/** Where the user revokes a token at the provider (the platform cannot). */
export const REVOKE_URL = 'https://claude.ai/settings';
const CONCURRENT = new Set(['version_conflict', 'epoch_stale']);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TOKEN_RE = /^[!-~]{1,4096}$/;
// The full login of KOR-71 (ADR-092) adds the refresh token, its scopes and plan, and the mark of a refused renewal.
const FULL_KEYS = ['refresh_token', 'scopes', 'subscription_type', 'rate_limit_tier', 'relogin'];
const BODY_KEYS = ['token', 'email', 'kind', 'expires_at', 'name', 'note', 'use', 'mailbox', ...FULL_KEYS];
const HOST_RE = /^[A-Za-z0-9.-]{1,253}$/;
const IMAP_USER_RE = /^[ -~]{1,256}$/;
const KINDS = ['setup_token', 'login_token', 'oauth_login'];
const PLAN_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const SCOPE_RE = /^[a-z_:]{1,64}$/;
const MAX_SCOPES = 32;

export interface SubscriptionUse {
  readonly pool: boolean;
  readonly orgs: readonly string[];
}

/** The subscription's own mailbox (`contracts/credentials/mailbox.json`, KOR-73). */
export interface Mailbox {
  readonly imap_host: string;
  readonly imap_port: number;
  readonly imap_user: string;
  readonly imap_password: string;
  readonly email: string;
}

export const isMailbox = (value: unknown): value is Mailbox =>
  isRecord(value) &&
  Object.keys(value).length === 5 &&
  typeof value.imap_host === 'string' &&
  HOST_RE.test(value.imap_host) &&
  Number.isSafeInteger(value.imap_port) &&
  (value.imap_port as number) >= 1 &&
  (value.imap_port as number) <= 65535 &&
  typeof value.imap_user === 'string' &&
  IMAP_USER_RE.test(value.imap_user) &&
  typeof value.imap_password === 'string' &&
  value.imap_password.length >= 1 &&
  value.imap_password.length <= 1024 &&
  typeof value.email === 'string' &&
  value.email.length <= 254 &&
  EMAIL_RE.test(value.email);

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
  /** The address of the subscription's own mailbox (never its password), null without one. */
  readonly mailbox: string | null;
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

const isPlan = (value: unknown): boolean => value === undefined || (typeof value === 'string' && PLAN_RE.test(value));

/** The fields of a full login (ADR-092), each optional. */
function isFullLogin(value: Record<string, unknown>): boolean {
  const scopes = value.scopes;
  return (
    (value.refresh_token === undefined ||
      (typeof value.refresh_token === 'string' && TOKEN_RE.test(value.refresh_token))) &&
    (scopes === undefined ||
      (Array.isArray(scopes) &&
        scopes.length <= MAX_SCOPES &&
        scopes.every((item) => typeof item === 'string' && SCOPE_RE.test(item)))) &&
    isPlan(value.subscription_type) &&
    isPlan(value.rate_limit_tier) &&
    (value.relogin === undefined || value.relogin === 'renewal_refused')
  );
}

/** A body of `contracts/credentials/claude_subscription.json` (also of a further subscription). */
export function isSubscriptionRecord(value: unknown): value is Record<string, unknown> {
  if (!isRecord(value) || !('email' in value) || !('kind' in value)) return false;
  if (!Object.keys(value).every((key) => BODY_KEYS.includes(key))) return false;
  const email = value.email;
  return (
    KINDS.includes(String(value.kind)) &&
    isFullLogin(value) &&
    (email === null || (typeof email === 'string' && email.length <= 254 && EMAIL_RE.test(email))) &&
    (value.token === undefined || (typeof value.token === 'string' && TOKEN_RE.test(value.token))) &&
    (value.expires_at === undefined || typeof value.expires_at === 'string') &&
    (value.name === undefined || isLabel(value.name, 64)) &&
    (value.note === undefined || isLabel(value.note, 500)) &&
    (value.use === undefined || isUse(value.use)) &&
    (value.mailbox === undefined || isMailbox(value.mailbox))
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
    const blank = { name: null, note: null, email: null, use: null, mailbox: null };
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
    mailbox: isMailbox(body.mailbox) ? body.mailbox.email : null,
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
export async function subscriptionDevices(context: TasksContext): Promise<{
  readonly devices: readonly DeviceChoice[];
  readonly preselected: string | null;
  /** The preselected device runs logins side by side with the subscription's own mailbox (KOR-73). */
  readonly parallel: boolean;
}> {
  const found = await connectDevices(context);
  const eligible = await eligibleDevices(context);
  const having = (feature: string) =>
    new Set(eligible.filter((item) => (item.features ?? []).includes(feature)).map((item) => item.device_id));
  const able = having(FEATURE);
  const parallel = having(PARALLEL_FEATURE);
  const devices = found.devices.filter((item) => able.has(item.device));
  const preselected = devices.find((item) => item.online)?.device ?? null;
  return { devices, preselected, parallel: preselected !== null && parallel.has(preselected) };
}

export const newSubscriptionId = (): string => uuid7();

/** The data of a new account (registration, ADR-098 section 5); never stored. */
export interface Account {
  readonly name: string;
  readonly birth_date?: string;
  readonly consents?: readonly Consent[];
}

/** A further subscription written before its login (KOR-73): label, note, address, own mailbox and use. */
export interface NewRecord {
  readonly name: string;
  readonly note?: string;
  readonly email?: string;
  readonly mailbox?: Mailbox;
  readonly orgs?: readonly string[];
}

export interface LoginInput {
  readonly subscriptionId: string;
  readonly mode: LoginMode;
  readonly name?: string;
  readonly note?: string;
  readonly email?: string;
  /** KOR-73, a parallel device only: the data of a new account (automatic). */
  readonly account?: Account;
  /** KOR-73, a parallel device only: the record to write before the login (a new subscription). */
  readonly create?: NewRecord;
  /** KOR-73: the subscription holds its own mailbox – automatic on a parallel device, the relay otherwise. */
  readonly ownMailbox?: boolean;
}

const trimmed = (value: string | undefined): string | undefined => {
  const text = value?.trim();
  return text === undefined || text === '' ? undefined : text;
};

/** The sealed body of a new record (`createSubscription`); `invalid_argument` when it breaks the contract. */
export function newRecordBody(input: NewRecord): Record<string, unknown> {
  const orgs = [...new Set(input.orgs ?? [])].sort();
  const body: Record<string, unknown> = {
    email: trimmed(input.email) ?? null,
    kind: 'oauth_login',
    name: input.name.trim(),
  };
  const note = trimmed(input.note);
  if (note !== undefined) body.note = note;
  if (input.mailbox !== undefined) body.mailbox = { ...input.mailbox };
  if (orgs.length > 0) body.use = { pool: false, orgs };
  if (!isSubscriptionRecord(body)) refuse('invalid_argument');
  return body;
}

/** Writes a new further subscription without a token (module comment); `PUT` with `expected_version` 0. */
export async function createSubscription(context: RequestsContext, id: string, input: NewRecord): Promise<void> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const body = newRecordBody(input);
  const latest = await currentKey(context, userId, identity);
  const header = newHeader({
    id,
    owner: userId,
    kind: KIND,
    version: 1,
    epoch: latest.epoch,
    author: userId,
    ts: formatTime(context.now()),
  });
  const record = await sealCredential(latest.key, header, bodyJson(body), identity.sig.privateKey);
  const use = isUse(body.use) ? body.use : { pool: false, orgs: [] };
  await context.client.request('PUT', `/v1/subscriptions/${encodeURIComponent(id)}`, {
    body: { record, expected_version: 0, use },
  });
}

/** Seals and posts `claude.login` for a further subscription to `device` (module comment). */
export async function startSubscriptionLogin(
  context: RequestsContext,
  device: string,
  input: LoginInput,
  parallel = false,
): Promise<PreparedRequest> {
  // a subscription with its own mailbox, a record written first and a registration need a device that reads them
  const extended = input.ownMailbox === true || input.create !== undefined || input.account !== undefined;
  if (extended && !parallel) refuse('no_device_available');
  const mode: LoginMode = input.ownMailbox === true ? 'automatic' : input.mode;
  const body: Record<string, unknown> = { mode, subscription_id: input.subscriptionId };
  if (parallel) body.extended = true;
  if (input.create !== undefined) await createSubscription(context, input.subscriptionId, input.create);
  for (const field of ['name', 'note', 'email'] as const) {
    const value = trimmed(input[field]);
    if (value !== undefined && (field !== 'email' || mode === 'automatic')) body[field] = value;
  }
  if (mode === 'automatic' && input.account !== undefined) body.account = accountBody(input.account);
  const request = { device, kind: 'claude.login' as const, body };
  const prepared = await prepareRequest(context, parallel ? { ...request, slot: input.subscriptionId } : request);
  await submitRequest(context, prepared);
  return prepared;
}

function accountBody(account: Account): Record<string, unknown> {
  const body: Record<string, unknown> = { name: account.name.trim() };
  const birth = trimmed(account.birth_date);
  if (birth !== undefined) body.birth_date = birth;
  if (account.consents !== undefined && account.consents.length > 0) body.consents = [...new Set(account.consents)];
  return body;
}

/** One entry of "add several subscriptions" (KOR-73). */
export interface BatchEntry {
  readonly record: NewRecord;
  readonly account?: Account;
}

export type BatchStarted =
  | { readonly subscriptionId: string; readonly prepared: PreparedRequest }
  | { readonly subscriptionId: string; readonly error: string };

/** Starts one login per entry at once on a parallel device (module comment); each entry has its own outcome. */
export function addSubscriptions(
  context: RequestsContext,
  device: string,
  entries: readonly BatchEntry[],
): Promise<BatchStarted[]> {
  return Promise.all(
    entries.map(async (entry): Promise<BatchStarted> => {
      const subscriptionId = newSubscriptionId();
      const input: LoginInput = {
        subscriptionId,
        mode: entry.record.mailbox === undefined ? 'manual' : 'automatic',
        create: entry.record,
        ...(entry.account !== undefined && { account: entry.account }),
      };
      try {
        return { subscriptionId, prepared: await startSubscriptionLogin(context, device, input, true) };
      } catch (error) {
        return { subscriptionId, error: errorCode(error) };
      }
    }),
  );
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
  if (opened.primary || (answer as Record<string, unknown>).primary !== false) refuse('invalid_argument');
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
    // nothing left that a device could renew (ADR-092)
    delete body.token;
    delete body.refresh_token;
    delete body.relogin;
  });
}

/** Sets the own mailbox of a further subscription, or removes it (`null`). */
export function setSubscriptionMailbox(context: RequestsContext, id: string, mailbox: Mailbox | null): Promise<void> {
  if (mailbox !== null && !isMailbox(mailbox)) refuse('invalid_argument');
  return changeSubscription(context, id, (body) => {
    delete body.mailbox;
    if (mailbox !== null) body.mailbox = { ...mailbox };
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
