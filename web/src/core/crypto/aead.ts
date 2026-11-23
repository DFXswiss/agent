// AES-256-GCM with a 12-byte nonce and a 16-byte tag (04-crypto.md section 1), twin of `agent_hub.crypto.aead`.
// Keys are non-extractable `CryptoKey`s; `aesKey` imports raw key bytes once.
// `sealBlob`/`openBlob` produce and read the `nonce(12) || ciphertext` form of identity bundles and packages.
import { concat, copy } from '../encoding/bytes';
import { fail } from './errors';
import { Secret } from './secret';
import { attempt, random, subtle } from './subtle';

export const KEY_BYTES = 32;
export const NONCE_BYTES = 12;
export const TAG_BYTES = 16;

/** A non-extractable AES-256-GCM key from 32 raw bytes (`header_invalid` otherwise). */
export async function aesKey(raw: Uint8Array | Secret): Promise<CryptoKey> {
  const bytes = raw instanceof Secret ? raw.reveal((value) => value) : copy(raw);
  if (bytes.length !== KEY_BYTES) fail('header_invalid');
  return subtle.importKey('raw', bytes, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
}

function checkKey(key: CryptoKey, nonce: Uint8Array): void {
  const algorithm = key.algorithm as Partial<AesKeyAlgorithm>;
  if (algorithm.name !== 'AES-GCM' || algorithm.length !== KEY_BYTES * 8 || nonce.length !== NONCE_BYTES) {
    fail('header_invalid');
  }
}

/** 12 random bytes. */
export function randomNonce(): Uint8Array<ArrayBuffer> {
  return random(NONCE_BYTES);
}

/** `ciphertext || tag`; an AES-256-GCM key and a 12-byte nonce (`header_invalid` otherwise). */
export async function seal(
  key: CryptoKey,
  nonce: Uint8Array,
  plaintext: Uint8Array,
  aad: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  checkKey(key, nonce);
  const params = { name: 'AES-GCM', iv: copy(nonce), additionalData: copy(aad) };
  return new Uint8Array(await subtle.encrypt(params, key, copy(plaintext)));
}

/** The plaintext, or `decrypt_failed` for a wrong key, nonce, AAD or a tampered ciphertext. */
export async function open(
  key: CryptoKey,
  nonce: Uint8Array,
  ciphertext: Uint8Array,
  aad: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  checkKey(key, nonce);
  const params = { name: 'AES-GCM', iv: copy(nonce), additionalData: copy(aad) };
  const plain = await attempt(() => subtle.decrypt(params, key, copy(ciphertext)));
  if (plain === null) fail('decrypt_failed');
  return new Uint8Array(plain);
}

/** `nonce || AES-GCM(key, plaintext, aad)` with a random nonce unless one is given (test vectors). */
export async function sealBlob(
  key: CryptoKey,
  plaintext: Uint8Array,
  aad: Uint8Array,
  nonce: Uint8Array = randomNonce(),
): Promise<Uint8Array<ArrayBuffer>> {
  return concat(nonce, await seal(key, nonce, plaintext, aad));
}

/** The inverse of `sealBlob`; anything shorter than nonce and tag is `decrypt_failed`. */
export async function openBlob(key: CryptoKey, blob: Uint8Array, aad: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  if (blob.length < NONCE_BYTES + TAG_BYTES) fail('decrypt_failed');
  return open(key, blob.subarray(0, NONCE_BYTES), blob.subarray(NONCE_BYTES), aad);
}
