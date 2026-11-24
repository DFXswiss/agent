// Derivations of 04-crypto.md section 3 (HKDF-SHA256, 32-byte output, empty salt unless stated), twin of
// `agent_hub.crypto.kdf`. Derived keys are returned as non-extractable `CryptoKey`s (AES-GCM, HMAC) wherever the
// value is a key; public derivations (`invite_id`, `recovery_id`) and the `sk_inv` seed return bytes.
import { concat, copy, utf8 } from '../encoding/bytes';
import { fail } from './errors';
import * as L from './labels';
import { Secret } from './secret';
import { sha256, subtle } from './subtle';

export const KEY_BYTES = 32;
export const CODE_BYTES = 16;
export const SALT_BYTES = 32;

type Ikm = Uint8Array | Secret;

function bytesOf(ikm: Ikm): Uint8Array<ArrayBuffer> {
  return ikm instanceof Secret ? ikm.reveal((raw) => raw) : copy(ikm);
}

function base(ikm: Ikm): Promise<CryptoKey> {
  return subtle.importKey('raw', bytesOf(ikm), 'HKDF', false, ['deriveBits', 'deriveKey']);
}

function params(info: Uint8Array, salt: Uint8Array): HkdfParams {
  return { name: 'HKDF', hash: 'SHA-256', salt: copy(salt), info: copy(info) };
}

/** HKDF-SHA256 (RFC 5869) as bytes; an empty salt is the all-zero salt of the RFC. */
export async function hkdf(
  ikm: Ikm,
  info: Uint8Array,
  salt: Uint8Array = new Uint8Array(0),
  length = KEY_BYTES,
): Promise<Uint8Array<ArrayBuffer>> {
  return new Uint8Array(await subtle.deriveBits(params(info, salt), await base(ikm), length * 8));
}

/** HKDF-SHA256 into a non-extractable AES-256-GCM key. */
export async function hkdfAesKey(ikm: Ikm, info: Uint8Array, salt: Uint8Array = new Uint8Array(0)): Promise<CryptoKey> {
  return subtle.deriveKey(params(info, salt), await base(ikm), { name: 'AES-GCM', length: 256 }, false, [
    'encrypt',
    'decrypt',
  ]);
}

/** HKDF-SHA256 into a non-extractable HMAC-SHA256 key (32 bytes). */
export async function hkdfHmacKey(ikm: Ikm, info: Uint8Array): Promise<CryptoKey> {
  const algorithm = { name: 'HMAC', hash: 'SHA-256', length: KEY_BYTES * 8 };
  return subtle.deriveKey(params(info, new Uint8Array(0)), await base(ikm), algorithm, false, ['sign', 'verify']);
}

function requireLength(ikm: Ikm, length: number, code: Parameters<typeof fail>[0]): Ikm {
  if (ikm.length !== length) fail(code);
  return ikm;
}

function requireSalt(salt: Uint8Array): Uint8Array {
  if (salt.length !== SALT_BYTES) fail('header_invalid');
  return salt;
}

/** `prf.eval.first` of every `create()` and `get()`: SHA-256 of the PRF label. */
export function prfInput(): Promise<Uint8Array<ArrayBuffer>> {
  return sha256(utf8(L.PRF_INPUT_LABEL));
}

/** `KEK_i` of a passkey slot; the PRF output must be 32 bytes (`prf_length`). */
export function kekFromPrf(prfOutput: Secret, credentialId: Uint8Array): Promise<CryptoKey> {
  return hkdfAesKey(requireLength(prfOutput, KEY_BYTES, 'prf_length'), concat(L.label(L.KEK), credentialId));
}

/** `KEK_rec`; the recovery code must be 16 bytes (`recovery_code_length`). */
export function kekFromRecoveryCode(code: Uint8Array): Promise<CryptoKey> {
  return hkdfAesKey(requireLength(code, CODE_BYTES, 'recovery_code_length'), L.label(L.RECOVERY_KEK));
}

/** The 16 raw bytes behind `recovery_id`. */
export async function recoveryIdRaw(code: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return (await hkdf(requireLength(code, CODE_BYTES, 'recovery_code_length'), L.label(L.RECOVERY_ID))).slice(0, 16);
}

/** The 16 raw bytes behind `invite_id`; the code must be 16 bytes (`invite_code_length`). */
export async function inviteIdRaw(code: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return (await hkdf(requireLength(code, CODE_BYTES, 'invite_code_length'), L.label(L.INVITE_ID))).slice(0, 16);
}

/** The AES-256-GCM key of the invitation package. */
export function packageKey(code: Uint8Array): Promise<CryptoKey> {
  return hkdfAesKey(requireLength(code, CODE_BYTES, 'invite_code_length'), L.label(L.INVITE_PACKAGE));
}

/** The Ed25519 seed of `sk_inv`. */
export async function inviteSignSeed(code: Uint8Array): Promise<Secret> {
  return Secret.of(await hkdf(requireLength(code, CODE_BYTES, 'invite_code_length'), L.label(L.INVITE_SIGN)));
}

/** `k_event = HKDF(scope_key, salt=header.salt, info=P||"event")`; scope key and salt 32 bytes. */
export function eventKey(scopeKey: Secret, salt: Uint8Array): Promise<CryptoKey> {
  return hkdfAesKey(requireLength(scopeKey, KEY_BYTES, 'header_invalid'), L.label(L.EVENT), requireSalt(salt));
}

/** `dk_e = HKDF(WK_e, info=P||"dedupe")` as HMAC-SHA256 key. */
export function dedupeKey(workgroupKey: Secret): Promise<CryptoKey> {
  return hkdfHmacKey(requireLength(workgroupKey, KEY_BYTES, 'header_invalid'), L.label(L.DEDUPE));
}

/** `k_cred = HKDF(CK_e, salt=header.salt, info=P||"credential")`. */
export function credentialKey(credentialScopeKey: Secret, salt: Uint8Array): Promise<CryptoKey> {
  const ikm = requireLength(credentialScopeKey, KEY_BYTES, 'header_invalid');
  return hkdfAesKey(ikm, L.label(L.CREDENTIAL), requireSalt(salt));
}

/** The runner storage key `HKDF(device_sig_seed, info=P||"runner-storage")` (AES-256-GCM). */
export function storageKey(deviceSigSeed: Secret): Promise<CryptoKey> {
  return hkdfAesKey(requireLength(deviceSigSeed, KEY_BYTES, 'header_invalid'), L.label(L.RUNNER_STORAGE));
}
