// Invitation codes and packages (04-crypto.md sections 3 and 4.6), twin of `agent_hub.crypto.invite`, after Spike D
// `web/e2ee.js` (`inviteSecrets`, `formatCode`, `parseCode`, `sealPackage`, `openPackage`).
//
// Code: 16 random bytes, shown as base32 (RFC 4648, uppercase, no padding) in groups `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`.
// The parser strips `-` and spaces; a character outside the alphabet (checked before uppercasing) is
// `invite_code_invalid`, a wrong length `invite_code_length`, non-zero trailing bits `invite_code_invalid`.
// Package: `b64u(nonce || AES-GCM(package_key, body_json(package), AAD = P || "invite-package\0" || invite_id))`.
// A package that does not open is `invite_code_invalid`; one with a wrong structure is `header_invalid`. A user
// invitation may carry the inviter's personal `message` (rule E-28, KOR-64; `isMessageText`), the one optional key.
// Registration proof (ADR-043): `inv_sig = Ed25519_{sk_inv}(P || "invite-register\0" || user_id || "\0" || challenge)`
// over the raw 32-byte challenge of `register/begin`; only the holder of the code can register through an invitation.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { bodyJson, isPlainObject, isPrintableAscii, parseJson } from '../encoding/json';
import * as aead from './aead';
import { isMessageText } from './agent';
import { CryptoError, fail } from './errors';
import * as F from './fields';
import { CODE_BYTES, inviteIdRaw, inviteSignSeed, packageKey } from './kdf';
import { ed25519KeyPair, ed25519Sign, ed25519VerifyB64u, type KeyPair, RAW_KEY_BYTES } from './keys';
import { label, LABEL_INVITE_PACKAGE, LABEL_INVITE_REGISTER } from './labels';
import { random } from './subtle';

export const CODE_CHARS = 26;
const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const BASE32_ANY_CASE_RE = /^[A-Za-z2-7]*$/;
const HUB_KEY_ID_RE = /^[a-z0-9-]{1,64}$/;
export const PACKAGE_VERSION = 1;
export const PACKAGE_KEYS = ['org', 'org_name', 'wg_names', 'founder', 'head', 'keys', 'hub_key', 'v'];
export const PACKAGE_MESSAGE_KEY = 'message';
const HEAD_KEYS = ['seq', 'hash'];
const KEY_KEYS = ['scope', 'scope_id', 'epoch', 'key'];
const HUB_KEY_KEYS = ['id', 'public_key'];
export const PACKAGE_SCOPES = ['org', 'wg'];
export const HASH_BYTES = 32;
export const CHALLENGE_BYTES = 32;

type LengthError = 'invite_code_length' | 'recovery_code_length';

/** 16 random bytes. */
export function generateCode(): Uint8Array<ArrayBuffer> {
  return random(CODE_BYTES);
}

/** `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX` (26 base32 characters); `lengthError` for a code that is not 16 bytes. */
export function formatCodeText(code: Uint8Array, lengthError: LengthError): string {
  if (code.length !== CODE_BYTES) fail(lengthError);
  let bits = '';
  for (const byte of code) bits += byte.toString(2).padStart(8, '0');
  let text = '';
  for (let at = 0; at < bits.length; at += 5) text += BASE32[parseInt(bits.slice(at, at + 5).padEnd(5, '0'), 2)];
  return `${[0, 5, 10, 15].map((at) => text.slice(at, at + 5)).join('-')}-${text.slice(20)}`;
}

/** The text form of an invitation code. */
export function formatCode(code: Uint8Array): string {
  return formatCodeText(code, 'invite_code_length');
}

/** The 16 bytes of a code text; `lengthError` names the code for a wrong length. */
export function parseCodeText(text: unknown, lengthError: LengthError): Uint8Array<ArrayBuffer> {
  if (typeof text !== 'string') fail('invite_code_invalid');
  const clean = text.replace(/[- ]/g, '');
  if (!BASE32_ANY_CASE_RE.test(clean)) fail('invite_code_invalid');
  if (clean.length !== CODE_CHARS) fail(lengthError);
  const bits = [...clean.toUpperCase()].map((char) => BASE32.indexOf(char).toString(2).padStart(5, '0')).join('');
  // 26 characters carry 130 bits: the last 2 must be zero
  if (bits.slice(CODE_BYTES * 8) !== '00') fail('invite_code_invalid');
  return Uint8Array.from({ length: CODE_BYTES }, (_, index) => parseInt(bits.slice(index * 8, index * 8 + 8), 2));
}

/** The 16 bytes of an invitation code text. */
export function parseCode(text: unknown): Uint8Array<ArrayBuffer> {
  return parseCodeText(text, 'invite_code_length');
}

/** What a code yields: `invite_id` (the only value the hub sees), `package_key` and `sk_inv`/`pk_inv`. */
export interface InviteSecrets {
  readonly inviteId: string;
  readonly packageKey: CryptoKey;
  readonly signing: KeyPair;
  /** b64u of the 32-byte Ed25519 public key of `sk_inv`. */
  readonly pkInv: string;
}

/** `b64u(HKDF(code, info=P||"invite-id")[:16])`. */
export async function inviteId(code: Uint8Array): Promise<string> {
  return b64uEncode(await inviteIdRaw(code));
}

/** All derivations of a 16-byte code (`invite_code_length` otherwise). */
export async function inviteSecrets(code: Uint8Array): Promise<InviteSecrets> {
  const id = await inviteId(code);
  const seed = await inviteSignSeed(code);
  const signing = await seed.reveal((raw) => ed25519KeyPair(raw));
  return { inviteId: id, packageKey: await packageKey(code), signing, pkInv: b64uEncode(signing.publicKey) };
}

/** `P || "invite-package\0" || invite_id`. */
export function packageAad(inviteIdText: unknown): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_INVITE_PACKAGE), utf8(F.b64uBytes(inviteIdText, 16)));
}

/** Exact key sets and value formats of the package (section 4.6). */
export function checkPackage(value: unknown): F.Fields {
  const withMessage = isPlainObject(value) && PACKAGE_MESSAGE_KEY in value;
  const pkg = F.requireKeys(value, withMessage ? [...PACKAGE_KEYS, PACKAGE_MESSAGE_KEY] : PACKAGE_KEYS);
  if (withMessage && !isMessageText(pkg.message)) fail('header_invalid');
  F.literal(pkg.v, PACKAGE_VERSION);
  F.uuid(pkg.org);
  if (typeof pkg.org_name !== 'string') fail('header_invalid');
  const names = pkg.wg_names;
  if (!isPlainObject(names) || !Object.values(names).every((name) => typeof name === 'string')) fail('header_invalid');
  Object.keys(names).forEach(F.uuid);
  F.b64uBytes(pkg.founder, RAW_KEY_BYTES);
  const head = F.requireKeys(pkg.head, HEAD_KEYS);
  F.requireInt(head.seq, 0);
  F.b64uBytes(head.hash, HASH_BYTES);
  const keys = pkg.keys;
  if (!Array.isArray(keys) || keys.length === 0) fail('header_invalid');
  for (const item of keys) {
    const entry = F.requireKeys(item, KEY_KEYS);
    if (!PACKAGE_SCOPES.includes(entry.scope as string)) fail('header_invalid');
    F.uuid(entry.scope_id);
    F.requireInt(entry.epoch, 1);
    F.b64uBytes(entry.key, 32);
  }
  const hubKey = F.requireKeys(pkg.hub_key, HUB_KEY_KEYS);
  if (typeof hubKey.id !== 'string' || !HUB_KEY_ID_RE.test(hubKey.id)) fail('header_invalid');
  F.b64uBytes(hubKey.public_key, RAW_KEY_BYTES);
  return pkg;
}

/** Encrypts a checked package under the code's package key; `nonce` only for test vectors. */
export async function sealPackage(secrets: InviteSecrets, pkg: unknown, nonce?: Uint8Array): Promise<string> {
  const plaintext = bodyJson(checkPackage(pkg));
  return b64uEncode(await aead.sealBlob(secrets.packageKey, plaintext, packageAad(secrets.inviteId), nonce));
}

/** Decrypts (`invite_code_invalid` if the code does not open it) and checks the package. */
export async function openPackage(secrets: InviteSecrets, sealed: string): Promise<F.Fields> {
  const blob = b64uDecode(sealed);
  let plaintext: Uint8Array | null;
  try {
    plaintext = await aead.openBlob(secrets.packageKey, blob, packageAad(secrets.inviteId));
  } catch (error) {
    if (!(error instanceof CryptoError)) throw error;
    plaintext = null;
  }
  if (plaintext === null) fail('invite_code_invalid');
  return checkPackage(parseJson(plaintext));
}

/** `P || "invite-register\0" || user_id || "\0" || challenge` (the registration proof, ADR-043). */
export function registerText(userId: unknown, challenge: Uint8Array): Uint8Array<ArrayBuffer> {
  if (typeof userId !== 'string' || userId === '') fail('header_invalid');
  if (!isPrintableAscii(userId)) fail('non_ascii');
  return concat(label(LABEL_INVITE_REGISTER), utf8(`${userId}\0`), F.requireBytes(challenge, CHALLENGE_BYTES));
}

/** The b64u `inv_sig` of `register/finish` by `sk_inv` of the code. */
export async function signRegister(secrets: InviteSecrets, userId: string, challenge: Uint8Array): Promise<string> {
  return b64uEncode(await ed25519Sign(secrets.signing.privateKey, registerText(userId, challenge)));
}

/** Checks `inv_sig` under the invitation's `pk_inv` (`signature_invalid`). */
export async function verifyRegister(
  pkInv: Uint8Array,
  userId: string,
  challenge: Uint8Array,
  invSig: unknown,
): Promise<void> {
  await ed25519VerifyB64u(pkInv, invSig, registerText(userId, challenge));
}
