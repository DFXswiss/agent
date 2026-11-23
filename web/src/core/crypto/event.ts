// Events (04-crypto.md section 4.3; checks E1, E2, E4 and E8 of section 6.4), twin of `agent_hub.crypto.event`.
// Header key set exactly `HEADER_KEYS`; `ct = AES-GCM(k_event, plaintext, AAD = canonical(header))` with
// `k_event = HKDF(scope_key, salt = header.salt, info = P || "event")`; `sig = Ed25519_author(P || "event-sig\0" ||
// canonical(header) || ct_raw)`. Kind/scope/author rules, rights and epochs (E3, E5-E7, E9) are in src/core/log.
import { fail } from './errors';
import * as F from './fields';
import { eventKey, SALT_BYTES } from './kdf';
import { LABEL_EVENT_SIG } from './labels';
import { Secret } from './secret';
import * as signed from './signed';

export const VERSION = 1;
export const ALG = 'A256GCM-HKDF';
export const HEADER_KEYS = [
  'v',
  'alg',
  'id',
  'org',
  'scope',
  'scope_id',
  'epoch',
  'wg',
  'task',
  'session',
  'author',
  'kind',
  'ref',
  'ts',
  'lh',
  'salt',
  'nonce',
] as const;
export const SCOPES = ['org', 'wg'] as const;
/** E8: `ct` at most 256 KiB decoded (ciphertext including the 16-byte tag). */
export const MAX_CT_BYTES = 256 * 1024;
/** E8: terminal chunks (`session.terminal`) at most 64 KiB decoded. */
export const MAX_TERMINAL_CT_BYTES = 64 * 1024;
export const TERMINAL_KIND = 'session.terminal';
/** The only org-scope kind whose header may name a workgroup in `wg`. */
export const WG_PROFILE_KIND = 'wg.profile';
const REF_RE = /^[A-Za-z0-9._:/-]{1,200}$/;

/** E1/E2: exact key set, `v`, `alg`, value formats and the scope/scope_id/wg relation. */
export function checkHeader(value: unknown): F.Fields {
  const header = F.headerDict(value, HEADER_KEYS);
  F.literal(header.v, VERSION);
  F.literal(header.alg, ALG);
  F.uuid(header.id);
  const org = F.uuid(header.org);
  const scopeId = F.uuid(header.scope_id);
  const wg = F.uuidOrEmpty(header.wg);
  F.uuidOrEmpty(header.task);
  F.uuidOrEmpty(header.session);
  F.uuid(header.author);
  F.kind(header.kind);
  if (header.ref !== '' && !(typeof header.ref === 'string' && REF_RE.test(header.ref))) fail('header_invalid');
  F.time(header.ts);
  F.requireInt(header.epoch, 1);
  F.requireInt(header.lh, 0);
  F.b64uBytes(header.salt, SALT_BYTES);
  F.b64uBytes(header.nonce, 12);
  if (!(header.scope === 'org' || header.scope === 'wg') || scopeId !== (header.scope === 'wg' ? wg : org)) {
    fail('header_invalid');
  }
  if (header.scope === 'org' && wg !== '' && header.kind !== WG_PROFILE_KIND) fail('header_invalid');
  return header;
}

/** The E8 limit for this header's kind. */
export function maxCtBytes(header: F.Fields): number {
  return header.kind === TERMINAL_KIND ? MAX_TERMINAL_CT_BYTES : MAX_CT_BYTES;
}

export const FORMAT: signed.Format = { checkHeader, deriveKey: eventKey, sigLabel: LABEL_EVENT_SIG, maxCt: maxCtBytes };

export interface HeaderInput {
  id: string;
  org: string;
  scope: string;
  scope_id: string;
  epoch: number;
  author: string;
  kind: string;
  ts: string;
  lh: number;
  wg?: string;
  task?: string;
  session?: string;
  ref?: string;
}

/** The checked header fields without `salt` and `nonce` (`sealEvent` draws those). */
export function newHeader(input: HeaderInput): F.Fields {
  const { wg = '', task = '', session = '', ref = '', ...rest } = input;
  return signed.checkFields(FORMAT, { v: VERSION, alg: ALG, ...rest, wg, task, session, ref });
}

/** Encrypts and signs an event (writer, section 5.12); the plaintext is the body JSON of the kind. */
export function sealEvent(
  scopeKey: Secret,
  fields: unknown,
  plaintext: Uint8Array,
  author: CryptoKey,
  fixedSaltNonce?: readonly [Uint8Array, Uint8Array],
): Promise<signed.Sealed> {
  return signed.seal(FORMAT, scopeKey, fields, plaintext, author, fixedSaltNonce);
}

/** E1, E2, E4, E8 without any key (what the hub checks); returns the checked header. */
export async function verifyEvent(event: unknown, authorSigPublic: Uint8Array): Promise<F.Fields> {
  return (await signed.verify(FORMAT, event, authorSigPublic)).header;
}

/** `verifyEvent`, then decrypt with the scope key of the header's epoch. */
export function openEvent(scopeKey: Secret, event: unknown, authorSigPublic: Uint8Array): Promise<Uint8Array> {
  return signed.open(FORMAT, scopeKey, event, authorSigPublic);
}
