// Community messages and the community key (12-community.md section 3, ADR-061), twin of
// `agent_hub.crypto.community`.
//
// A message has the shape of events and credential records (`./signed`): header key set exactly `HEADER_KEYS`;
// `ct = AES-GCM(k_msg, body, AAD = canonical(header))` with `k_msg = HKDF(CMK_e, salt = header.salt, info = P ||
// "community")`; `sig = Ed25519_author(P || "community-sig\0" || canonical(header) || ct_raw)`. The body is body JSON
// `{"name", "text", "v"}` with the reader rules of `checkBody`. Wraps of `CMK_e` are ordinary wraps (`./wrap`) of the
// scope `community` with the fixed scope ID `SCOPE_ID`; the key check is `HMAC-SHA256(CMK_e, P ||
// "community-check\0" || canonical({"epoch": e}))`; a key request is signed over `P || "community-request\0" ||
// canonical({"id", "kem_pub", "user"})`.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat } from '../encoding/bytes';
import { bodyJson, canonical, parseJson } from '../encoding/json';
import { fail } from './errors';
import * as F from './fields';
import { hkdfAesKey, KEY_BYTES, SALT_BYTES } from './kdf';
import { ed25519Sign, ed25519VerifyB64u, type KeyPair } from './keys';
import { label } from './labels';
import { Secret } from './secret';
import * as signed from './signed';
import { hmacSha256 } from './subtle';
import * as wrap from './wrap';

export const VERSION = 1;
export const ALG = 'A256GCM-HKDF';
export const HEADER_KEYS = ['v', 'alg', 'id', 'author', 'epoch', 'ts', 'salt', 'nonce'];
export const BODY_KEYS = ['name', 'text', 'v'];
export const MAX_CT_BYTES = 16 * 1024;
export const MAX_TEXT_CHARS = 2000;
export const MAX_NAME_CHARS = 64;
export const SCOPE = 'community';
export const SCOPE_ID = '00000000-0000-0000-0000-000000000000';
export const INFO = 'community';
export const LABEL_SIG = 'community-sig\0';
export const LABEL_CHECK = 'community-check\0';
export const LABEL_REQUEST = 'community-request\0';

/** Characters a display name must not contain: Unicode categories Cc, Cf, Zl, Zp. */
const NAME_FORBIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;
const NAME_FORBIDDEN_ALL = new RegExp(NAME_FORBIDDEN.source, 'gu');
/** Controls except line feed and tab, bidi controls, zero-width characters, U+2028/U+2029 and the BOM. */
const TEXT_FORBIDDEN = /[\p{Cc}\u202A-\u202E\u2066-\u2069\u200B-\u200F\u2028\u2029\uFEFF]/u;
const TEXT_ALLOWED_CONTROLS = /[\n\t]/g;
/** Python's `str.isspace` set, so that "no surrounding white space" means exactly what the hub's twin means. */
const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PADDED = new RegExp(`^[${PY_SPACE}]|[${PY_SPACE}]$`, 'u');
const PADDING = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'gu');

/** The body of a message as readers accept it. */
export interface Body {
  readonly name: string;
  readonly text: string;
  readonly v: 1;
}

/** The 32-byte key check of epoch `epoch`. */
export async function keyCheck(key: Secret, epoch: number): Promise<Uint8Array<ArrayBuffer>> {
  if (key.length !== KEY_BYTES) fail('header_invalid');
  const text = concat(label(LABEL_CHECK), canonical({ epoch: F.requireInt(epoch, 1) }));
  return key.reveal((raw) => hmacSha256(raw, text));
}

export interface RequestFields {
  readonly id: string;
  readonly kem_pub: string;
  readonly user: string;
}

/** `P || "community-request\0" || canonical({id, kem_pub, user})`. */
export function requestText(request: RequestFields): Uint8Array<ArrayBuffer> {
  F.uuid(request.id);
  F.uuid(request.user);
  F.b64uBytes(request.kem_pub, 32);
  return concat(label(LABEL_REQUEST), canonical({ id: request.id, kem_pub: request.kem_pub, user: request.user }));
}

/** The user's b64u signature of a key request. */
export async function signRequest(identity: CryptoKey, request: RequestFields): Promise<string> {
  return b64uEncode(await ed25519Sign(identity, requestText(request)));
}

/** `k_msg = HKDF(CMK_e, salt = header.salt, info = P || "community")`. */
export function messageKey(key: Secret, salt: Uint8Array): Promise<CryptoKey> {
  if (key.length !== KEY_BYTES || salt.length !== SALT_BYTES) fail('header_invalid');
  return hkdfAesKey(key, label(INFO), salt);
}

/** Exact key set, `v`, `alg` and value formats (`header_invalid`). */
export function checkHeader(value: unknown): F.Fields {
  const header = F.headerDict(value, HEADER_KEYS);
  F.literal(header.v, VERSION);
  F.literal(header.alg, ALG);
  F.uuid(header.id);
  F.uuid(header.author);
  F.requireInt(header.epoch, 1);
  F.time(header.ts);
  F.b64uBytes(header.salt, SALT_BYTES);
  F.b64uBytes(header.nonce, 12);
  return header;
}

export const FORMAT: signed.Format = {
  checkHeader,
  deriveKey: messageKey,
  sigLabel: LABEL_SIG,
  maxCt: () => MAX_CT_BYTES,
};

export interface HeaderInput {
  readonly id: string;
  readonly author: string;
  readonly epoch: number;
  readonly ts: string;
}

/** The checked header fields without `salt` and `nonce` (`sealMessage` draws those). */
export function newHeader(input: HeaderInput): F.Fields {
  return signed.checkFields(FORMAT, { v: VERSION, alg: ALG, ...input });
}

const codePoints = (text: string): number => [...text].length;

function trimmed(value: unknown, limit: number): string {
  if (typeof value !== 'string' || PADDED.test(value)) fail('header_invalid');
  const length = codePoints(value);
  if (length < 1 || length > limit) fail('header_invalid');
  return value;
}

/** A display name: 1-64 code points, trimmed, NFC, none of the categories Cc, Cf, Zl, Zp. */
export function checkName(value: unknown): string {
  const name = trimmed(value, MAX_NAME_CHARS);
  if (name.normalize('NFC') !== name || NAME_FORBIDDEN.test(name)) fail('header_invalid');
  return name;
}

/** A message text: 1-2000 code points, trimmed, no control but line feed and tab, no bidi or zero-width character. */
export function checkText(value: unknown): string {
  const text = trimmed(value, MAX_TEXT_CHARS);
  if (TEXT_FORBIDDEN.test(text.replace(TEXT_ALLOWED_CONTROLS, ''))) fail('header_invalid');
  return text;
}

/** The body JSON of a message: exact keys, `v` 1, a valid `name` and `text`. */
export function checkBody(value: unknown): Body {
  const body = F.requireKeys(value, BODY_KEYS);
  F.literal(body.v, VERSION);
  return { name: checkName(body.name), text: checkText(body.text), v: VERSION };
}

/** The writer's normalisation of a name: forbidden characters removed, NFC, surrounding white space trimmed. */
export function cleanName(value: string): string {
  return value.replace(NAME_FORBIDDEN_ALL, '').normalize('NFC').replace(PADDING, '');
}

/** The writer's normalisation of a text: CR LF to LF, forbidden characters removed, surrounding white space trimmed. */
export function cleanText(value: string): string {
  return value
    .replace(/\r\n?/g, '\n')
    .replace(/[^\n\t]/gu, (char) => (TEXT_FORBIDDEN.test(char) ? '' : char))
    .replace(PADDING, '');
}

/** Body JSON (sorted keys, no white space, UTF-8) of a checked body. */
export function bodyBytes(name: string, text: string): Uint8Array<ArrayBuffer> {
  return bodyJson(checkBody({ name, text, v: VERSION }));
}

/** Encrypts the body under `CMK_e` and signs as the author; fresh salt and nonce (fixed ones only for vectors). */
export function sealMessage(
  key: Secret,
  fields: unknown,
  body: Uint8Array,
  author: CryptoKey,
  fixedSaltNonce?: readonly [Uint8Array, Uint8Array],
): Promise<signed.Sealed> {
  return signed.seal(FORMAT, key, fields, body, author, fixedSaltNonce);
}

/** Structure, header, size and signature without any key; returns the checked header. */
export async function verifyMessage(message: unknown, authorSigPublic: Uint8Array): Promise<F.Fields> {
  return (await signed.verify(FORMAT, message, authorSigPublic)).header;
}

/** `verifyMessage`, decrypt and check the body (`decrypt_failed`, `header_invalid`). */
export async function openMessage(key: Secret, message: unknown, authorSigPublic: Uint8Array): Promise<Body> {
  const plaintext = await signed.open(FORMAT, key, message, authorSigPublic);
  return checkBody(parseJson(plaintext));
}

const target = (epoch: number, recipient: string): wrap.WrapTarget => ({
  scope: SCOPE,
  scope_id: SCOPE_ID,
  epoch,
  recipient,
});

/** Wraps `CMK_e` for one recipient (a user ID for a self-wrap, a request ID for a request wrap). */
export function wrapCommunityKey(
  recipientKemPublic: Uint8Array,
  epoch: number,
  recipient: string,
  key: Secret,
  ephemeral?: Uint8Array,
): Promise<string> {
  return wrap.wrapKey(recipientKemPublic, target(epoch, recipient), key, ephemeral);
}

/** Opens a community key wrap. */
export function unwrapCommunityKey(recipient: KeyPair, epoch: number, to: string, text: string): Promise<Secret> {
  return wrap.unwrapKey(recipient, target(epoch, to), text);
}

/** The wrapper's signature of a community key wrap. */
export function signCommunityWrap(wrapper: CryptoKey, epoch: number, recipient: string, text: string): Promise<string> {
  return wrap.signWrap(wrapper, target(epoch, recipient), text);
}

/** Checks the wrapper's signature (`signature_invalid`). */
export function verifyCommunityWrap(
  wrapperSigPublic: Uint8Array,
  epoch: number,
  recipient: string,
  text: string,
  signature: unknown,
): Promise<void> {
  return wrap.verifyWrap(wrapperSigPublic, target(epoch, recipient), text, signature);
}

/** Checks a key request signature (`signature_invalid`, `header_invalid`). */
export async function verifyRequest(userSigPublic: Uint8Array, request: RequestFields, signature: unknown) {
  await ed25519VerifyB64u(userSigPublic, signature, requestText(request));
}

/** Compares a key with the key check of its epoch. */
export async function matchesCheck(key: Secret, epoch: number, check: string): Promise<boolean> {
  const expected = b64uDecode(check, 32);
  const actual = await keyCheck(key, epoch);
  return actual.every((byte, index) => byte === expected[index]);
}
