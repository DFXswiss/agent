// Raw key formats (04-crypto.md sections 1 and 2), twin of `agent_hub.crypto.keys`: X25519 and Ed25519 as 32 raw
// bytes, P-256 session keys as the 65-byte uncompressed point with raw `r || s` signatures (`agent-session-v1`).
// Private keys become non-extractable `CryptoKey`s; their raw bytes stay only inside an `Identity` (for re-sealing
// the bundle) and inside the closures that import them.
import { b64uDecode, b64uEncode, isB64u } from '../encoding/b64u';
import { concat, copy, fromHex, toHex } from '../encoding/bytes';
import { fail } from './errors';
import { Secret } from './secret';
import { attempt, subtle } from './subtle';

export const RAW_KEY_BYTES = 32;
export const ED25519_SIGNATURE_BYTES = 64;
export const P256_POINT_BYTES = 65;
export const P256_SIGNATURE_BYTES = 64;
export const P256_ORDER = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n;
export const ED25519_ORDER = 2n ** 252n + 27742317777372353535851937790883648493n;
// Small-order Ed25519 public keys (the libsodium blacklist, twin of `agent_hub.crypto.keys`): the canonical encodings
// of the 8 torsion points plus the non-canonical y = p and y = p + 1, compared with the sign bit (top bit of byte 31)
// cleared. Some WebCrypto implementations accept `R = key, S = 0` under such a key for every message.
const ED25519_SMALL_ORDER_KEYS = new Set([
  '0000000000000000000000000000000000000000000000000000000000000000',
  '0100000000000000000000000000000000000000000000000000000000000000',
  '26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05',
  'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a',
  'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
  'edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
  'eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
]);

// PKCS#8 prefixes of a raw 32-byte X25519 / Ed25519 private key (RFC 8410), after Spike D `web/e2ee.js`.
const PKCS8_X25519 = fromHex('302e020100300506032b656e04220420');
const PKCS8_ED25519 = fromHex('302e020100300506032b657004220420');

/** A private key as non-extractable `CryptoKey` with its raw public key. */
export interface KeyPair {
  readonly privateKey: CryptoKey;
  readonly publicKey: Uint8Array<ArrayBuffer>;
}

async function importPrivate(raw: Uint8Array, prefix: Uint8Array, name: string, usages: KeyUsage[]): Promise<KeyPair> {
  if (raw.length !== RAW_KEY_BYTES) fail('header_invalid');
  const pkcs8 = concat(prefix, raw);
  // the public key is read from a short-lived extractable copy; the kept key is not extractable
  const jwk = await subtle.exportKey('jwk', await subtle.importKey('pkcs8', pkcs8, { name }, true, usages));
  const privateKey = await subtle.importKey('pkcs8', pkcs8, { name }, false, usages);
  return { privateKey, publicKey: b64uDecode(jwk.x, RAW_KEY_BYTES) };
}

/** An X25519 key pair from the 32 raw private bytes (`header_invalid` otherwise). */
export function x25519KeyPair(raw: Uint8Array): Promise<KeyPair> {
  return importPrivate(raw, PKCS8_X25519, 'X25519', ['deriveBits']);
}

/** An Ed25519 key pair from the 32-byte seed (`header_invalid` otherwise). */
export function ed25519KeyPair(seed: Uint8Array): Promise<KeyPair> {
  return importPrivate(seed, PKCS8_ED25519, 'Ed25519', ['sign']);
}

async function generateRaw(name: string, usages: KeyUsage[]): Promise<Uint8Array<ArrayBuffer>> {
  const pair = (await subtle.generateKey({ name }, true, usages)) as CryptoKeyPair;
  return b64uDecode((await subtle.exportKey('jwk', pair.privateKey)).d, RAW_KEY_BYTES);
}

/** The 64-byte Ed25519 signature (deterministic). */
export async function ed25519Sign(privateKey: CryptoKey, message: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return new Uint8Array(await subtle.sign({ name: 'Ed25519' }, privateKey, copy(message)));
}

/** Whether a 32-byte Ed25519 public key encodes a small-order point (either sign bit). */
export function ed25519SmallOrder(publicKey: Uint8Array): boolean {
  return ED25519_SMALL_ORDER_KEYS.has(toHex(concat(publicKey.slice(0, 31), new Uint8Array([publicKey[31]! & 0x7f]))));
}

/** The little-endian scalar `S` of a 64-byte Ed25519 signature. */
function ed25519S(signature: Uint8Array): bigint {
  return signature.slice(RAW_KEY_BYTES).reduceRight((value, byte) => (value << 8n) | BigInt(byte), 0n);
}

/**
 * Verifies an Ed25519 signature; a wrong signature, key or length is `signature_invalid`. Stricter than WebCrypto
 * (ATS-03): a small-order public key and a non-canonical `S >= L` are refused before `subtle.verify`, identically to
 * `agent_hub.crypto.keys.ed25519_verify`.
 */
export async function ed25519Verify(publicKey: Uint8Array, signature: Uint8Array, message: Uint8Array): Promise<void> {
  if (publicKey.length !== RAW_KEY_BYTES || signature.length !== ED25519_SIGNATURE_BYTES) fail('signature_invalid');
  if (ed25519SmallOrder(publicKey) || ed25519S(signature) >= ED25519_ORDER) fail('signature_invalid');
  const valid = await attempt(async () => {
    const key = await subtle.importKey('raw', copy(publicKey), { name: 'Ed25519' }, false, ['verify']);
    return subtle.verify({ name: 'Ed25519' }, key, copy(signature), copy(message));
  });
  if (valid !== true) fail('signature_invalid');
}

/** `ed25519Verify` with the signature as b64u text (a structure field); malformed text is `signature_invalid`. */
export async function ed25519VerifyB64u(publicKey: Uint8Array, signature: unknown, message: Uint8Array): Promise<void> {
  if (!isB64u(signature)) fail('signature_invalid');
  await ed25519Verify(publicKey, b64uDecode(signature), message);
}

/** The key pairs of a user or a device: X25519 `kem` and Ed25519 `sig`. */
export class Identity {
  readonly #raw: Secret;

  private constructor(
    readonly kem: KeyPair,
    readonly sig: KeyPair,
    raw: Secret,
  ) {
    this.#raw = raw;
  }

  /** From the 32-byte X25519 private key and the 32-byte Ed25519 seed. */
  static async fromRaw(kem: Uint8Array, sig: Uint8Array): Promise<Identity> {
    return new Identity(await x25519KeyPair(kem), await ed25519KeyPair(sig), Secret.of(concat(kem, sig)));
  }

  /** Fresh random key pairs. */
  static async generate(): Promise<Identity> {
    return Identity.fromRaw(await generateRaw('X25519', ['deriveBits']), await generateRaw('Ed25519', ['sign']));
  }

  /** The 32-byte X25519 public key. */
  kemPublic(): Uint8Array<ArrayBuffer> {
    return copy(this.kem.publicKey);
  }

  /** The 32-byte Ed25519 public key. */
  sigPublic(): Uint8Array<ArrayBuffer> {
    return copy(this.sig.publicKey);
  }

  /** `{"kem": b64u, "sig": b64u}` as in log entries (`pub`). */
  public(): { kem: string; sig: string } {
    return { kem: b64uEncode(this.kem.publicKey), sig: b64uEncode(this.sig.publicKey) };
  }

  /** Calls `consume` with copies of the raw X25519 private key and Ed25519 seed (to seal the identity bundle). */
  revealPrivate<T>(consume: (kem: Uint8Array<ArrayBuffer>, sig: Uint8Array<ArrayBuffer>) => T): T {
    return this.#raw.reveal((raw) => consume(raw.slice(0, RAW_KEY_BYTES), raw.slice(RAW_KEY_BYTES)));
  }

  /** Signs with the Ed25519 identity key. */
  sign(message: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
    return ed25519Sign(this.sig.privateKey, message);
  }

  toJSON(): { kem: string; sig: string } {
    return this.public();
  }
}

/** A browser session key (P-256, non-extractable private key) and its 65-byte uncompressed public point. */
export async function generateSessionKey(): Promise<KeyPair> {
  const pair = (await subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign'])) as CryptoKeyPair;
  return { privateKey: pair.privateKey, publicKey: new Uint8Array(await subtle.exportKey('raw', pair.publicKey)) };
}

/** ECDSA P-256/SHA-256 as raw `r || s` (64 bytes). */
export async function p256SignRaw(privateKey: CryptoKey, message: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return new Uint8Array(await subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, privateKey, copy(message)));
}

function scalar(raw: Uint8Array): bigint {
  return raw.reduce((value, byte) => (value << 8n) | BigInt(byte), 0n);
}

/** Verifies a raw `r || s` signature under a 65-byte uncompressed point; any failure is `signature_invalid`. */
export async function p256VerifyRaw(point: Uint8Array, signature: Uint8Array, message: Uint8Array): Promise<void> {
  if (point.length !== P256_POINT_BYTES || point[0] !== 0x04 || signature.length !== P256_SIGNATURE_BYTES) {
    fail('signature_invalid');
  }
  const r = scalar(signature.subarray(0, 32));
  const s = scalar(signature.subarray(32));
  if (r < 1n || r >= P256_ORDER || s < 1n || s >= P256_ORDER) fail('signature_invalid');
  const valid = await attempt(async () => {
    const key = await subtle.importKey('raw', copy(point), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
    return subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, key, copy(signature), copy(message));
  });
  if (valid !== true) fail('signature_invalid');
}
