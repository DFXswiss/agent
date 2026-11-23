// The only access to WebCrypto (04-crypto.md section 1: WebCrypto only, no other crypto library). The same code runs
// in Node (Vitest) and in the browser; nothing here imports the Node crypto module.
import { copy } from '../encoding/bytes';

export const subtle: SubtleCrypto = globalThis.crypto.subtle;

/** `n` random bytes. */
export function random(n: number): Uint8Array<ArrayBuffer> {
  return globalThis.crypto.getRandomValues(new Uint8Array(n));
}

/** SHA-256. */
export async function sha256(data: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return new Uint8Array(await subtle.digest('SHA-256', copy(data)));
}

/** HMAC-SHA256 with a raw key (used where RFC 9180 chains raw values; keys of this protocol use `CryptoKey`s). */
export async function hmacSha256(key: Uint8Array, data: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  const imported = await subtle.importKey('raw', copy(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await subtle.sign('HMAC', imported, copy(data)));
}

/** Runs `operation` and maps any rejection to `null` (the caller turns it into a protocol error code). */
export async function attempt<T>(operation: () => Promise<T>): Promise<T | null> {
  try {
    return await operation();
  } catch {
    return null;
  }
}
