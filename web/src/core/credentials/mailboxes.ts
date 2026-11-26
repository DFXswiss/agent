// The user's mailboxes read by the platform (KOR-74, ADR-099; 03-api.md section 4.8; twin of the agent tool's
// `agent-platform mailbox`). A mailbox is a `mailbox` credential record sealed under the user's `CK`; the user keeps
// several, the first one is the primary mailbox of the automatic login.
//
// - `loadMailboxes`: `GET /v1/me/mailboxes` (metadata and the hub's plaintext mirror of the release), then every record
//   (`GET /v1/mailboxes/{id}`), checked like every own record: the answer names the user, the header the record, the
//   user and the kind; the author is the user or a device the verified logs entitle to the user's `CK` (or the user's
//   active agent); signature, the user's `CK` of the record's epoch, a body of `contracts/credentials/mailbox.json`
//   (`isMailboxBody`). Only name, purpose, address, host, port, user and the sealed release leave this module – never
//   the password. A record that does not open is `unverifiable` with its code. The primary one first, then by name.
// - `addMailbox`: a new record (a new UUIDv7, version 1, `expected_version` 0) sealed under the newest own `CK`, `PUT
//   /v1/mailboxes/{id}` with the mirror of its release `use {wgs}`.
// - `changeMailbox`: an own mailbox (the primary one too) opened, changed and sealed again under the newest own `CK`
//   (version + 1, author the user), `PUT` with the mirror of its sealed release; one retry after a concurrent write
//   (`version_conflict`, `epoch_stale`). `updateMailbox` (the form; an empty password keeps the stored one) and
//   `releaseMailbox` (the workgroups whose sessions may read it, ADR-099 section 3; none = released nowhere: the
//   sealed body has no `use`, the mirror is `{wgs: []}`) build on it.
// - `startMailboxCheck`: `mailbox.check {mailbox: <body>}` to a device – the body is opened here and sealed to the
//   device; the UI never holds the password.
// - `removeMailbox`: `DELETE /v1/mailboxes/{id}` (`404` counts as done).
// - `releasableWorkgroups`: the active workgroups of the user's organizations where the user has explicit access (the
//   runner honours a release only there, ADR-099 section 3).
import { uuid7 } from '../encoding/ids';
import { bodyJson, parseJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, openCredential, sealCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import { authorKey } from '../github/connection';
import { workgroupOrder } from '../org/members';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { isMailboxBody, isMailboxUse, MAILBOX_NAME_RE } from '../requests/bodies';
import {
  type PreparedRequest,
  prepareRequest,
  RequestError,
  type RequestsContext,
  submitRequest,
} from '../requests/client';
import { orgEntry, type OrgsState } from '../store/slices/orgs';
import { listOwnKeys, type OwnKey } from './records';

export const KIND = 'mailbox';
export const DEFAULT_PORT = '993';
export const PURPOSE_MAX = 500;
const CONCURRENT = new Set(['version_conflict', 'epoch_stale']);

export type MailboxStatus = 'verified' | 'unverifiable';

export interface Mailbox {
  readonly id: string;
  readonly primary: boolean;
  readonly status: MailboxStatus;
  /** The crypto or form code of an `unverifiable` record. */
  readonly code: string | null;
  readonly name: string | null;
  readonly purpose: string | null;
  readonly email: string | null;
  readonly host: string | null;
  readonly port: number | null;
  readonly user: string | null;
  /** The workgroups of the sealed release (empty: released nowhere, and for an unverifiable record). */
  readonly wgs: readonly string[];
}

/** The fields of the form (texts as typed; the port as text). */
export interface MailboxForm {
  readonly name: string;
  readonly purpose: string;
  readonly email: string;
  readonly imap_host: string;
  readonly imap_port: string;
  readonly imap_user: string;
  /** Empty when a stored mailbox is changed: the stored password stays. */
  readonly imap_password: string;
}

export const EMPTY_FORM: MailboxForm = {
  name: '',
  purpose: '',
  email: '',
  imap_host: '',
  imap_port: DEFAULT_PORT,
  imap_user: '',
  imap_password: '',
};

function refuse(code: string): never {
  throw new RequestError(code);
}

export const isMailboxName = (value: string): boolean => MAILBOX_NAME_RE.test(value);

/** The form of a stored mailbox, password empty (write-only). */
export function formOf(item: Mailbox): MailboxForm {
  return {
    name: item.name ?? '',
    purpose: item.purpose ?? '',
    email: item.email ?? '',
    imap_host: item.host ?? '',
    imap_port: item.port === null ? DEFAULT_PORT : String(item.port),
    imap_user: item.user ?? '',
    imap_password: '',
  };
}

/**
 * The body of a form (texts trimmed except the password; an empty purpose left out), or null when the contract does not
 * allow it. An empty password takes `stored` (a change keeps the stored password), else the form is not complete.
 */
export function formBody(values: MailboxForm, stored: string | null = null): Record<string, unknown> | null {
  const port = values.imap_port.trim();
  const purpose = values.purpose.trim();
  const body: Record<string, unknown> = {
    imap_host: values.imap_host.trim(),
    imap_port: /^[0-9]{1,5}$/.test(port) ? Number(port) : 0,
    imap_user: values.imap_user.trim(),
    imap_password: values.imap_password === '' ? stored : values.imap_password,
    email: values.email.trim(),
    name: values.name.trim(),
  };
  if (purpose !== '') body.purpose = purpose;
  return isMailboxBody(body) ? body : null;
}

/** Whether `name` is taken by another own mailbox than `except`. */
export const nameTaken = (items: readonly Mailbox[], name: string, except: string | null = null): boolean =>
  items.some((item) => item.id !== except && item.name === name.trim());

/** Whether the form can be saved: a valid body (a change may leave the password empty) and a free name. */
export function formReady(values: MailboxForm, items: readonly Mailbox[], changing: string | null): boolean {
  return formBody(values, changing === null ? null : 'kept') !== null && !nameTaken(items, values.name, changing);
}

// --- the list ---

interface Meta {
  readonly id: string;
  readonly primary: boolean;
}

const isMeta = (value: unknown): boolean =>
  isRecord(value) &&
  typeof value.id === 'string' &&
  typeof value.primary === 'boolean' &&
  Number.isSafeInteger(value.version) &&
  (value.use === null || isMailboxUse(value.use));

/** `GET /v1/me/mailboxes`; a malformed answer is `unavailable`. */
export async function listMailboxes(context: RequestsContext): Promise<Meta[]> {
  const body = await context.client.request('GET', '/v1/me/mailboxes');
  if (!isRecord(body) || !Array.isArray(body.mailboxes) || !body.mailboxes.every(isMeta)) {
    throw new HubError('unavailable', 200);
  }
  return body.mailboxes as Meta[];
}

/** `GET /v1/mailboxes/{id}`, or null when the record is gone. */
export async function fetchMailbox(context: RequestsContext, id: string): Promise<unknown> {
  try {
    return await context.client.request('GET', `/v1/mailboxes/${encodeURIComponent(id)}`);
  } catch (error) {
    if (error instanceof HubError && error.code === 'not_found') return null;
    throw error;
  }
}

export interface OpenedMailbox {
  readonly header: Fields;
  readonly primary: boolean;
  readonly body: Record<string, unknown>;
}

/** Verifies and opens the hub's answer for the own mailbox `id` (module comment). */
export async function openMailbox(
  context: RequestsContext,
  id: string,
  answer: unknown,
  keys: readonly OwnKey[],
): Promise<OpenedMailbox> {
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
  if (!isMailboxBody(body)) refuse('params_invalid');
  return { header: checked, primary: found.primary === true, body: body as unknown as Record<string, unknown> };
}

const errorCode = (error: unknown): string =>
  error instanceof RequestError || error instanceof HubError ? error.code : 'unverifiable';

const textOr = (value: unknown): string | null => (typeof value === 'string' ? value : null);

async function entryOf(context: RequestsContext, meta: Meta, keys: readonly OwnKey[]): Promise<Mailbox | null> {
  const answer = await fetchMailbox(context, meta.id);
  if (answer === null) return null;
  const base = { id: meta.id, primary: meta.primary };
  let opened: OpenedMailbox;
  try {
    opened = await openMailbox(context, meta.id, answer, keys);
  } catch (error) {
    const blank = { name: null, purpose: null, email: null, host: null, port: null, user: null, wgs: [] };
    return { ...base, ...blank, status: 'unverifiable', code: errorCode(error) };
  }
  const { body } = opened;
  return {
    ...base,
    status: 'verified',
    code: null,
    name: textOr(body.name),
    purpose: textOr(body.purpose),
    email: body.email as string,
    host: body.imap_host as string,
    port: body.imap_port as number,
    user: body.imap_user as string,
    wgs: isMailboxUse(body.use) ? body.use.wgs : [],
  };
}

const order = (a: Mailbox, b: Mailbox): number =>
  Number(b.primary) - Number(a.primary) ||
  Number(a.name === null) - Number(b.name === null) ||
  (a.name ?? '').localeCompare(b.name ?? '') ||
  Number(a.id > b.id) - Number(a.id < b.id);

/** Every own mailbox (module comment). */
export async function loadMailboxes(context: RequestsContext): Promise<Mailbox[]> {
  const listed = await listMailboxes(context);
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const keys = listed.length === 0 ? [] : (await listOwnKeys(context, userId, identity)).opening;
  const found: Mailbox[] = [];
  for (const meta of listed) {
    const entry = await entryOf(context, meta, keys);
    if (entry !== null) found.push(entry);
  }
  return found.sort(order);
}

// --- writes ---

const mirror = (body: Record<string, unknown>) => ({ wgs: isMailboxUse(body.use) ? [...body.use.wgs] : [] });

/** Seals `body` as version `version` of `id` under the newest own `CK` and puts it; false after a concurrent write. */
async function put(context: RequestsContext, id: string, version: number, body: Record<string, unknown>) {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const latest = (await listOwnKeys(context, userId, identity)).keys[0] ?? refuse('epoch_stale');
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
  try {
    await context.client.request('PUT', `/v1/mailboxes/${encodeURIComponent(id)}`, {
      body: { record, expected_version: version, use: mirror(body) },
    });
    return true;
  } catch (error) {
    if (error instanceof HubError && CONCURRENT.has(error.code)) return false;
    throw error;
  }
}

const releaseOf = (wgs: readonly string[]) => [...new Set(wgs)].sort();

/** The release `wgs` set into `body` (none: no `use`); `invalid_argument` for one the contract does not allow. */
function setRelease(body: Record<string, unknown>, wgs: readonly string[]): void {
  const use = { wgs: releaseOf(wgs) };
  if (!isMailboxUse(use)) refuse('invalid_argument');
  delete body.use;
  if (use.wgs.length > 0) body.use = use;
}

/** Adds a mailbox (module comment); `others` are the own mailboxes (the name must be free). Returns its ID. */
export async function addMailbox(
  context: RequestsContext,
  values: MailboxForm,
  wgs: readonly string[],
  others: readonly Mailbox[],
): Promise<string> {
  const body = formBody(values) ?? refuse('invalid_argument');
  if (nameTaken(others, values.name)) refuse('invalid_argument');
  setRelease(body, wgs);
  const id = uuid7(context.now());
  if (await put(context, id, 0, body)) return id;
  if (!(await put(context, id, 0, body))) refuse('version_conflict');
  return id;
}

async function changeOnce(
  context: RequestsContext,
  id: string,
  edit: (body: Record<string, unknown>) => void,
): Promise<boolean> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const answer = (await fetchMailbox(context, id)) ?? refuse('not_found');
  const keys = (await listOwnKeys(context, userId, identity)).opening;
  const opened = await openMailbox(context, id, answer, keys);
  const body = { ...opened.body };
  edit(body);
  return put(context, id, opened.header.version as number, body);
}

/** Changes an own mailbox (module comment). */
export async function changeMailbox(
  context: RequestsContext,
  id: string,
  edit: (body: Record<string, unknown>) => void,
): Promise<void> {
  if (await changeOnce(context, id, edit)) return;
  if (!(await changeOnce(context, id, edit))) refuse('version_conflict');
}

/** Saves the form over the stored mailbox `id`: an empty password keeps the stored one, the release stays. */
export function updateMailbox(
  context: RequestsContext,
  id: string,
  values: MailboxForm,
  others: readonly Mailbox[],
): Promise<void> {
  if (!formReady(values, others, id)) refuse('invalid_argument');
  return changeMailbox(context, id, (body) => {
    const next = formBody(values, body.imap_password as string)!;
    for (const field of ['name', 'purpose']) delete body[field];
    Object.assign(body, next);
  });
}

/** Releases the mailbox to exactly the workgroups `wgs` (none: released nowhere). */
export function releaseMailbox(context: RequestsContext, id: string, wgs: readonly string[]): Promise<void> {
  setRelease({}, wgs);
  return changeMailbox(context, id, (body) => setRelease(body, wgs));
}

export async function removeMailbox(context: RequestsContext, id: string): Promise<void> {
  try {
    await context.client.request('DELETE', `/v1/mailboxes/${encodeURIComponent(id)}`);
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'not_found') throw error;
  }
}

// --- the check on a device ---

/** Seals and posts `mailbox.check` with the opened body of the own mailbox `id` to `device`. */
export async function startMailboxCheck(
  context: RequestsContext,
  device: string,
  id: string,
): Promise<PreparedRequest> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const answer = (await fetchMailbox(context, id)) ?? refuse('not_found');
  const opened = await openMailbox(context, id, answer, (await listOwnKeys(context, userId, identity)).opening);
  const prepared = await prepareRequest(context, { device, kind: 'mailbox.check', body: { mailbox: opened.body } });
  await submitRequest(context, prepared);
  return prepared;
}

// --- the workgroups a mailbox can be released to ---

export interface ReleasableWorkgroup {
  readonly org: string;
  readonly wg: string;
}

/** The active workgroups of `orgIds` with a verified log where `userId` has explicit access (module comment). */
export function releasableWorkgroups(
  orgs: OrgsState,
  orgIds: readonly string[],
  userId: string | null,
): ReleasableWorkgroup[] {
  if (userId === null) return [];
  const found: ReleasableWorkgroup[] = [];
  for (const org of [...new Set(orgIds)]) {
    const state = orgEntry(orgs, org).log?.state;
    if (state === undefined) continue;
    for (const wg of workgroupOrder(state))
      if (state.writable(wg) && state.explicitAccess(userId, wg) !== null) found.push({ org, wg });
  }
  return found;
}
