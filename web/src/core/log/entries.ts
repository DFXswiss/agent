// Entries of the membership log (04-crypto.md section 4.7) and the platform log (section 4.8): exact body formats,
// signing texts, signatures, entry hashes and the builders of every entry type. TypeScript twin of
// `agent_hub.log.entries`.
//
// Entry `{"body": {...}, "sig": b64u}` (plus `"inv_sig"` exactly on `accept`). `sig = Ed25519_by(P || label ||
// canonical(body))` with label `log\0` (membership) or `platform-log\0` (platform). Entry hash
// `b64u(SHA-256(canonical(entry)))` over the whole entry including signatures; `prev` of the next entry is this hash.
//
// Format check order (every implementation): the entry is an object whose `body` is an object with a string `type`
// (`header_invalid`); known type (`log_type`); entry key set (`header_invalid`); exact body key set of the type
// (`header_invalid`); canonical JSON of the body (`non_ascii`, `header_invalid`); field formats, common fields first
// in the order `org, seq, prev, type, by, ts, v`, then the type's fields in the order of the contract
// (`header_invalid`). Entry signatures are not format: a malformed or wrong `sig`/`inv_sig` is a rule failure.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat } from '../encoding/bytes';
import { canonical, isPlainObject } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { CryptoError, type ErrorCode, fail } from '../crypto/errors';
import * as F from '../crypto/fields';
import { ed25519Sign, ed25519VerifyB64u, RAW_KEY_BYTES } from '../crypto/keys';
import { label, LABEL_LOG, LABEL_PLATFORM_LOG } from '../crypto/labels';
import { sha256 } from '../crypto/subtle';

export const VERSION = 1;
/** The `by` of every platform-log entry. */
export const PLATFORM_ROOT = 'platform-root';
export const MAX_EPOCH = 2 ** 31 - 1;
export const INVITE_ID_BYTES = 16;
export const SIGNATURE_BYTES = 64;

export type Body = Record<string, unknown>;

/** A signed log entry. */
export interface Entry {
  readonly body: Body;
  readonly sig: string;
  readonly inv_sig?: string;
}

type Check = (value: unknown) => unknown;

const ENTRY_KEYS = ['body', 'sig'];
const ACCEPT_ENTRY_KEYS = ['body', 'sig', 'inv_sig'];
const CAPACITY_KEYS = ['pool_ready', 'vm_memory_budget_mib', 'vm_memory_used_mib', 'vm_slots_free', 'vm_slots_total'];
const VERSION_TEXT_RE = /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/;
/** `os` and `arch` of the enrollment body (`contracts/api/device-enroll.request.json`). */
const PLATFORM_TEXT_RE = /^[a-z0-9_]{1,32}$/;

const seq: Check = (value) => F.requireInt(value, 0);
const epoch: Check = (value) => F.requireInt(value, 1, MAX_EPOCH);
const prev: Check = (value) => (value === '' ? value : F.b64uBytes(value, 32));
const key: Check = (value) => F.b64uBytes(value, RAW_KEY_BYTES);
const bodySig: Check = (value) => F.b64uBytes(value, SIGNATURE_BYTES); // inside a body (device_certify)
const inviteId: Check = (value) => F.b64uBytes(value, INVITE_ID_BYTES);
const version: Check = (value) => F.literal(value, VERSION);
/** Identity proofs of `admit` (ADR-060): `""` when not used. */
const sigOrEmpty: Check = (value) => (value === '' ? value : bodySig(value));

function pub(value: unknown): unknown {
  const keys = F.requireKeys(value, ['kem', 'sig']);
  key(keys.kem);
  return key(keys.sig);
}

function oneOf(...allowed: string[]): Check {
  return (value) => {
    if (!allowed.includes(value as string)) fail('header_invalid');
    return value;
  };
}

function bool(value: unknown): unknown {
  if (typeof value !== 'boolean') fail('header_invalid');
  return value;
}

function text(pattern: RegExp): Check {
  return (value) => {
    if (typeof value !== 'string' || !pattern.test(value)) fail('header_invalid');
    return value;
  };
}

function capacity(value: unknown): unknown {
  const fields = F.requireKeys(value, CAPACITY_KEYS);
  for (const name of CAPACITY_KEYS) F.requireInt(fields[name], 0);
  return fields;
}

function wgs(access: Check): Check {
  return (value) => {
    if (!isPlainObject(value)) fail('header_invalid');
    for (const [wg, level] of Object.entries(value)) {
      F.uuid(wg);
      access(level);
    }
    return value;
  };
}

const WG_ACCESS = oneOf('read', 'write');

// Membership log: common fields, then the fields of each type (contracts/common.json log_body_*).
const LOG_COMMON: Record<string, Check> = {
  org: F.uuid,
  seq,
  prev,
  type: F.kind,
  by: F.uuid,
  ts: F.time,
  v: version,
};
const LOG_TYPE_FIELDS: Record<string, Record<string, Check>> = {
  genesis: { subject: F.uuid, pub, default_wg: F.uuid, private: bool },
  invite: {}, // user or device form, see typeFields
  invite_revoke: { invite_id: inviteId },
  accept: { invite_id: inviteId, subject: F.uuid, kind: oneOf('user', 'device'), pub },
  role: { subject: F.uuid, role: oneOf('admin', 'member') },
  wg_create: { wg: F.uuid, epoch },
  wg_archive: { wg: F.uuid },
  grant: { subject: F.uuid, wg: F.uuid, access: WG_ACCESS },
  ungrant: { subject: F.uuid, wg: F.uuid },
  device_assign: { device: F.uuid, wg: F.uuid }, // optional platform_seq, see typeFields
  device_unassign: { device: F.uuid, wg: F.uuid },
  device_assign_org: { device: F.uuid }, // optional platform_seq, see typeFields (ADR-075)
  device_unassign_org: { device: F.uuid },
  remove: { subject: F.uuid },
  rotate: { scope: oneOf('org', 'wg'), scope_id: F.uuid, epoch },
  admit: {
    subject: F.uuid,
    pub,
    platform_seq: seq,
    id_sig: sigOrEmpty,
    by_platform_seq: seq,
    by_id_sig: sigOrEmpty,
    role: oneOf('admin', 'member'),
    wgs: wgs(WG_ACCESS),
  },
};
const INVITE_COMMON: Record<string, Check> = { invite_id: inviteId, pk_inv: key, kind: oneOf('user', 'device') };
const INVITE_USER: Record<string, Check> = {
  ...INVITE_COMMON,
  role: oneOf('admin', 'member'),
  wgs: wgs(WG_ACCESS),
  expires: F.time,
};
const INVITE_DEVICE: Record<string, Check> = { ...INVITE_COMMON, wgs: wgs(oneOf('device')), expires: F.time };

// Platform log (contracts/common.json platform_body_*).
const PLATFORM_COMMON: Record<string, Check> = {
  seq,
  prev,
  type: F.kind,
  by: (value) => F.literal(value, PLATFORM_ROOT),
  ts: F.time,
  v: version,
};
/** Platform roles: `inviter` (ADR-026) and `operator` (receives the requests of ADR-079 §4). */
export const PLATFORM_ROLES = ['inviter', 'operator'] as const;
/** Switches of `platform_policy` (ADR-079 §2): off until an entry turns them on. */
export const PLATFORM_POLICIES = ['open_platform_access', 'open_platform_invites'] as const;
const PLATFORM_TYPE_FIELDS: Record<string, Record<string, Check>> = {
  root: { pub: key },
  device_invite: { invite_id: inviteId, pk_inv: key, expires: F.time },
  device_certify: {
    device: F.uuid,
    pub,
    invite_id: inviteId,
    enroll_sig: bodySig,
    inv_sig: bodySig,
    agent_version: text(VERSION_TEXT_RE),
    os: text(PLATFORM_TEXT_RE),
    arch: text(PLATFORM_TEXT_RE),
    capacity,
  },
  device_decertify: { device: F.uuid },
  role_grant: { subject: F.uuid, role: oneOf(...PLATFORM_ROLES) },
  role_revoke: { subject: F.uuid, role: oneOf(...PLATFORM_ROLES) },
  user_invite: { invite_id: inviteId, pk_inv: key, expires: F.time },
  org_platform_access: { org: F.uuid, granted: bool },
  user_certify: { user: F.uuid, pub },
  user_decertify: { user: F.uuid },
  // ADR-079: switches and the operator-subscription list
  platform_policy: { name: oneOf(...PLATFORM_POLICIES), on: bool },
  org_operator_subscriptions: { org: F.uuid, granted: bool },
};

export const LOG_TYPES: readonly string[] = Object.keys(LOG_TYPE_FIELDS);
export const PLATFORM_TYPES: readonly string[] = Object.keys(PLATFORM_TYPE_FIELDS);

function typeFields(body: Body): Record<string, Check> {
  const type = body.type as string;
  if (type === 'invite') return body.kind === 'device' ? INVITE_DEVICE : INVITE_USER;
  const own = { ...LOG_TYPE_FIELDS[type] };
  if ((type === 'device_assign' || type === 'device_assign_org') && Object.hasOwn(body, 'platform_seq'))
    own.platform_seq = seq;
  return own;
}

function bodyOfType(entry: unknown, entryKeys: (type: string) => string[], types: Record<string, unknown>): Body {
  if (!isPlainObject(entry) || !isPlainObject(entry.body)) fail('header_invalid');
  const body = entry.body;
  if (typeof body.type !== 'string') fail('header_invalid');
  if (!Object.hasOwn(types, body.type)) fail('log_type');
  F.requireKeys(entry, entryKeys(body.type));
  return body;
}

function checkFields(body: Body, checks: Record<string, Check>): Body {
  F.headerDict(body, Object.keys(checks));
  for (const [name, check] of Object.entries(checks)) check(body[name]);
  return body;
}

/** The body of a well-formed membership-log entry (format only; the rules are in `verify`). */
export function checkLogEntry(entry: unknown): Body {
  const body = bodyOfType(entry, (type) => (type === 'accept' ? ACCEPT_ENTRY_KEYS : ENTRY_KEYS), LOG_TYPE_FIELDS);
  return checkFields(body, { ...LOG_COMMON, ...typeFields(body) });
}

/** The body of a well-formed platform-log entry (format only; the rules are in `platform`). */
export function checkPlatformEntry(entry: unknown): Body {
  const body = bodyOfType(entry, () => ENTRY_KEYS, PLATFORM_TYPE_FIELDS);
  return checkFields(body, { ...PLATFORM_COMMON, ...PLATFORM_TYPE_FIELDS[body.type as string] });
}

/** `P || "log\0" || canonical(body)`: signed by `by` (and by `sk_inv` on `accept`). */
export function logText(body: Body): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_LOG), canonical(body));
}

/** `P || "platform-log\0" || canonical(body)`: signed by the platform root. */
export function platformText(body: Body): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_PLATFORM_LOG), canonical(body));
}

/** `b64u(SHA-256(canonical(entry)))` over the whole entry including its signatures. */
export async function entryHash(entry: unknown): Promise<string> {
  return b64uEncode(await sha256(canonical(entry)));
}

/** `{body, sig}`, plus `inv_sig` by `sk_inv` when `invite` is given (the `accept` entry). */
export async function signLogEntry(body: Body, author: CryptoKey, invite?: CryptoKey): Promise<Entry> {
  const message = logText(body);
  const sig = b64uEncode(await ed25519Sign(author, message));
  if (invite === undefined) return { body: { ...body }, sig };
  return { body: { ...body }, sig, inv_sig: b64uEncode(await ed25519Sign(invite, message)) };
}

/** `{body, sig}` signed by the platform root. */
export async function signPlatformEntry(body: Body, root: CryptoKey): Promise<Entry> {
  return { body: { ...body }, sig: b64uEncode(await ed25519Sign(root, platformText(body))) };
}

/** The code of a refusal; any error that is not a `CryptoError` is a defect and propagates. */
export function refusalCode(error: unknown): ErrorCode {
  if (error instanceof CryptoError) return error.code;
  throw error;
}

/** True if `check` completes, false if it fails with a `CryptoError`; any other error propagates. */
export async function succeeds(check: () => Promise<unknown>): Promise<boolean> {
  try {
    await check();
  } catch (error) {
    refusalCode(error);
    return false;
  }
  return true;
}

/** True if `signature` (b64u text) is a valid Ed25519 signature of `message` under `publicKey` (b64u). */
export function signatureValid(publicKey: string, signature: unknown, message: Uint8Array): Promise<boolean> {
  return succeeds(() => ed25519VerifyB64u(b64uDecode(publicKey), signature, message));
}

/** `(seq, hash)` of the last entry of a log (`hash` = `entryHash`), as in the API (`log_head`). */
export interface Head {
  readonly seq: number;
  readonly hash: string;
}

/**
 * L9: a log must extend the head the client verified before (`log_rollback` otherwise). `entries` is the complete
 * log from `seq` 0; it extends `stored` if it is at least as long and its entry at `stored.seq` has the stored hash.
 * Without a stored head every log extends.
 */
export async function checkExtends(stored: Head | null, entries: readonly unknown[]): Promise<void> {
  if (stored === null) return;
  if (entries.length <= stored.seq || (await entryHash(entries[stored.seq])) !== stored.hash) fail('log_rollback');
}

// --- builders ---

/** The fields of one membership-log entry without the common fields (`org`, `seq`, `prev`, `by`, `ts`, `v`). */
export type LogFields =
  | { type: 'genesis'; subject: string; pub: { kem: string; sig: string }; default_wg: string; private: boolean }
  | {
      type: 'invite';
      invite_id: string;
      pk_inv: string;
      kind: 'user';
      role: 'admin' | 'member';
      wgs: Record<string, 'read' | 'write'>;
      expires: string;
    }
  | {
      type: 'invite';
      invite_id: string;
      pk_inv: string;
      kind: 'device';
      wgs: Record<string, 'device'>;
      expires: string;
    }
  | { type: 'invite_revoke'; invite_id: string }
  | { type: 'accept'; invite_id: string; subject: string; kind: 'user' | 'device'; pub: { kem: string; sig: string } }
  | { type: 'role'; subject: string; role: 'admin' | 'member' }
  | { type: 'wg_create'; wg: string; epoch: 1 }
  | { type: 'wg_archive'; wg: string }
  | { type: 'grant'; subject: string; wg: string; access: 'read' | 'write' }
  | { type: 'ungrant'; subject: string; wg: string }
  | { type: 'device_assign'; device: string; wg: string; platform_seq?: number }
  | { type: 'device_unassign'; device: string; wg: string }
  | { type: 'device_assign_org'; device: string; platform_seq?: number }
  | { type: 'device_unassign_org'; device: string }
  | { type: 'remove'; subject: string }
  | { type: 'rotate'; scope: 'org' | 'wg'; scope_id: string; epoch: number }
  | {
      type: 'admit';
      subject: string;
      pub: { kem: string; sig: string };
      platform_seq: number;
      id_sig: string;
      by_platform_seq: number;
      by_id_sig: string;
      role: 'admin' | 'member';
      wgs: Record<string, 'read' | 'write'>;
    };

/** Where the next entry goes: the org and the head it extends (`null` before genesis). */
export interface Position {
  readonly org: string;
  readonly head: Head | null;
}

/** The body of the next entry after `position`, written by `by` at `ts` (header time; default: now). */
export function logBody(position: Position, by: string, fields: LogFields, ts: string = formatTime(Date.now())): Body {
  const { type, ...own } = fields;
  const { head } = position;
  return {
    org: position.org,
    seq: head === null ? 0 : head.seq + 1,
    prev: head === null ? '' : head.hash,
    type,
    by,
    ts,
    v: VERSION,
    ...own,
  };
}

/** A signer of membership-log entries: the author's principal ID and Ed25519 key (`invite`: `sk_inv` of `accept`). */
export interface LogSigner {
  readonly id: string;
  readonly key: CryptoKey;
  readonly invite?: CryptoKey;
}

/**
 * Builds and signs a chain of entries after `position`: each `prev` is the hash of the entry before it. Every entry
 * is checked against the format (a builder never produces an entry a verifier refuses for its form).
 */
export async function buildEntries(
  position: Position,
  signer: LogSigner,
  list: readonly LogFields[],
  ts?: string,
): Promise<Entry[]> {
  const out: Entry[] = [];
  let head = position.head;
  for (const fields of list) {
    const body = logBody({ org: position.org, head }, signer.id, fields, ts);
    const entry = await signLogEntry(body, signer.key, fields.type === 'accept' ? signer.invite : undefined);
    checkLogEntry(entry);
    out.push(entry);
    head = { seq: body.seq as number, hash: await entryHash(entry) };
  }
  return out;
}

/** The fields of one platform-log entry without the common fields. */
export type PlatformFields = { type: string } & Body;

/** Builds and signs a chain of platform-log entries after `head` (the operator side; used by tests and tools). */
export async function buildPlatformEntries(
  head: Head | null,
  root: CryptoKey,
  list: readonly PlatformFields[],
  ts: string = formatTime(Date.now()),
): Promise<Entry[]> {
  const out: Entry[] = [];
  let current = head;
  for (const { type, ...own } of list) {
    const body = {
      seq: current === null ? 0 : current.seq + 1,
      prev: current === null ? '' : current.hash,
      type,
      by: PLATFORM_ROOT,
      ts,
      v: VERSION,
      ...own,
    };
    const entry = await signPlatformEntry(body, root);
    checkPlatformEntry(entry);
    out.push(entry);
    current = { seq: body.seq, hash: await entryHash(entry) };
  }
  return out;
}
