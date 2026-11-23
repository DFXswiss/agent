// UUIDv7 identifiers (04-crypto.md section 1, RFC 9562): lowercase text with dashes, made by the object creator.
// Twin of `agent_hub.crypto.ids`.
import { fail } from '../crypto/errors';
import { toHex } from './bytes';

export const UUID7_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const RANDOM_BYTES = 10;
const MAX_MS = 2 ** 48 - 1;

/**
 * A UUIDv7: 48-bit Unix milliseconds, version 7, 12 + 62 random bits, RFC 4122 variant. `unixMs` and `random`
 * (10 bytes; the top 4 bits of byte 0 and the top 2 bits of byte 2 are replaced) exist for deterministic vectors.
 */
export function uuid7(unixMs: number = Date.now(), random?: Uint8Array): string {
  const rand = random ?? globalThis.crypto.getRandomValues(new Uint8Array(RANDOM_BYTES));
  if (!Number.isSafeInteger(unixMs) || unixMs < 0 || unixMs > MAX_MS || rand.length !== RANDOM_BYTES) {
    fail('header_invalid');
  }
  const raw = new Uint8Array(16);
  for (let index = 0; index < 6; index++) raw[index] = Math.floor(unixMs / 2 ** (8 * (5 - index))) & 0xff;
  raw.set(rand, 6);
  raw[6] = 0x70 | (raw[6]! & 0x0f);
  raw[8] = 0x80 | (raw[8]! & 0x3f);
  const text = toHex(raw);
  return `${text.slice(0, 8)}-${text.slice(8, 12)}-${text.slice(12, 16)}-${text.slice(16, 20)}-${text.slice(20)}`;
}

/** True for the lowercase text form of a version-7 UUID with the RFC 4122 variant. */
export function isUuid7(value: unknown): value is string {
  return typeof value === 'string' && UUID7_RE.test(value);
}

/** The Unix milliseconds of a UUIDv7 (`header_invalid` for anything else). */
export function uuid7Ms(value: unknown): number {
  if (!isUuid7(value)) fail('header_invalid');
  return parseInt(value.slice(0, 8) + value.slice(9, 13), 16);
}
