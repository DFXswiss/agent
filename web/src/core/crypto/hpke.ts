// HPKE RFC 9180 base mode, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-256-GCM, single shot, twin of
// `agent_hub.crypto.hpke` and adapted from Spike D `web/e2ee.js`. The key schedule chains raw HMAC values as the RFC
// does; the recipient's private key is a non-extractable X25519 `CryptoKey`. Output form: `enc(32) || ciphertext`.
import { concat, copy, utf8 } from '../encoding/bytes';
import { aesKey, open as aeadOpen, seal as aeadSeal, KEY_BYTES, NONCE_BYTES, TAG_BYTES } from './aead';
import { fail } from './errors';
import { type KeyPair, x25519KeyPair, RAW_KEY_BYTES } from './keys';
import { attempt, hmacSha256, random, subtle } from './subtle';

export const KEM_ID = 0x0020; // DHKEM(X25519, HKDF-SHA256)
export const KDF_ID = 0x0001; // HKDF-SHA256
export const AEAD_ID = 0x0002; // AES-256-GCM
const KEM_SUITE_ID = concat(utf8('KEM'), new Uint8Array([0x00, 0x20]));
const HPKE_SUITE_ID = concat(utf8('HPKE'), new Uint8Array([0x00, 0x20, 0x00, 0x01, 0x00, 0x02]));
export const ENC_BYTES = 32;
const HASH_BYTES = 32;
const EMPTY = new Uint8Array(0);
const VERSION = utf8('HPKE-v1');

function labeledExtract(salt: Uint8Array, label: string, ikm: Uint8Array, suite: Uint8Array) {
  return hmacSha256(salt.length ? salt : new Uint8Array(HASH_BYTES), concat(VERSION, suite, utf8(label), ikm));
}

async function labeledExpand(prk: Uint8Array, label: string, info: Uint8Array, length: number, suite: Uint8Array) {
  const labeled = concat(new Uint8Array([length >> 8, length & 0xff]), VERSION, suite, utf8(label), info);
  let out: Uint8Array<ArrayBuffer> = new Uint8Array(0);
  let block: Uint8Array<ArrayBuffer> = new Uint8Array(0);
  for (let counter = 1; out.length < length; counter++) {
    block = await hmacSha256(prk, concat(block, labeled, new Uint8Array([counter])));
    out = concat(out, block);
  }
  return out.slice(0, length);
}

async function sharedSecret(dh: Uint8Array, enc: Uint8Array, pkRm: Uint8Array) {
  const prk = await labeledExtract(EMPTY, 'eae_prk', dh, KEM_SUITE_ID);
  return labeledExpand(prk, 'shared_secret', concat(enc, pkRm), HASH_BYTES, KEM_SUITE_ID);
}

async function keySchedule(shared: Uint8Array, info: Uint8Array) {
  const pskIdHash = await labeledExtract(EMPTY, 'psk_id_hash', EMPTY, HPKE_SUITE_ID);
  const infoHash = await labeledExtract(EMPTY, 'info_hash', info, HPKE_SUITE_ID);
  const context = concat(new Uint8Array([0]), pskIdHash, infoHash);
  const secret = await labeledExtract(shared, 'secret', EMPTY, HPKE_SUITE_ID);
  return {
    key: await labeledExpand(secret, 'key', context, KEY_BYTES, HPKE_SUITE_ID),
    baseNonce: await labeledExpand(secret, 'base_nonce', context, NONCE_BYTES, HPKE_SUITE_ID),
  };
}

/** X25519; a small-order point yields the all-zero secret, which WebCrypto refuses (RFC 9180 7.1.4). */
async function exchange(privateKey: CryptoKey, publicRaw: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  const bits = await attempt(async () => {
    const pub = await subtle.importKey('raw', copy(publicRaw), { name: 'X25519' }, false, []);
    return subtle.deriveBits({ name: 'X25519', public: pub }, privateKey, 256);
  });
  if (bits === null) fail('decrypt_failed');
  return new Uint8Array(bits);
}

function requireKey(pkR: Uint8Array): Uint8Array {
  if (pkR.length !== ENC_BYTES) fail('header_invalid');
  return pkR;
}

/** The intermediate values of the sender setup (`shared_secret`, `key`, `base_nonce`), for test vectors only. */
export async function setupValues(
  ephemeral: Uint8Array,
  pkR: Uint8Array,
  info: Uint8Array,
): Promise<{ shared_secret: Uint8Array; key: Uint8Array; base_nonce: Uint8Array }> {
  const eph = await x25519KeyPair(ephemeral);
  const shared = await sharedSecret(await exchange(eph.privateKey, requireKey(pkR)), eph.publicKey, pkR);
  const { key, baseNonce } = await keySchedule(shared, info);
  return { shared_secret: shared, key, base_nonce: baseNonce };
}

/**
 * Single-shot seal to the 32-byte X25519 key `pkR` with empty AAD; returns `enc || ciphertext`.
 * `ephemeral` (32 raw private bytes) exists only for deterministic test vectors.
 */
export async function seal(
  pkR: Uint8Array,
  info: Uint8Array,
  plaintext: Uint8Array,
  ephemeral: Uint8Array = random(RAW_KEY_BYTES),
): Promise<Uint8Array<ArrayBuffer>> {
  requireKey(pkR);
  const eph = await x25519KeyPair(ephemeral);
  const { key, baseNonce } = await keySchedule(
    await sharedSecret(await exchange(eph.privateKey, pkR), eph.publicKey, pkR),
    info,
  );
  return concat(eph.publicKey, await aeadSeal(await aesKey(key), baseNonce, plaintext, EMPTY));
}

/** The inverse of `seal`: `hpke_short` below `enc` + tag, `decrypt_failed` for every other failure. */
export async function open(recipient: KeyPair, info: Uint8Array, sealed: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  if (sealed.length < ENC_BYTES + TAG_BYTES) fail('hpke_short');
  const enc = sealed.subarray(0, ENC_BYTES);
  const shared = await sharedSecret(await exchange(recipient.privateKey, enc), enc, recipient.publicKey);
  const { key, baseNonce } = await keySchedule(shared, info);
  return aeadOpen(await aesKey(key), baseNonce, sealed.subarray(ENC_BYTES), EMPTY);
}
