// Strict base64url without padding ("b64u", 04-crypto.md section 1), the twin of `agent_hub.crypto.encoding`'s
// `b64u_encode`/`is_b64u`/`b64u_decode`: only the url-safe alphabet, no padding, no whitespace, canonical trailing
// bits. Every violation is `header_invalid` (a malformed field of a structure).
import { fail } from '../crypto/errors';

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const VALUES = new Map([...ALPHABET].map((char, index) => [char, index]));

/** base64url without padding. */
export function b64uEncode(raw: Uint8Array): string {
  let out = '';
  for (let at = 0; at < raw.length; at += 3) {
    const chunk = (raw[at]! << 16) | ((raw[at + 1] ?? 0) << 8) | (raw[at + 2] ?? 0);
    const chars = Math.min(4, Math.ceil(((raw.length - at) * 8) / 6));
    for (let index = 0; index < chars; index++) out += ALPHABET[(chunk >> (18 - 6 * index)) & 63];
  }
  return out;
}

function decodeLoose(text: string): Uint8Array<ArrayBuffer> | null {
  if (text.length % 4 === 1) return null;
  const out = new Uint8Array(Math.floor((text.length * 6) / 8));
  let buffer = 0;
  let bits = 0;
  let at = 0;
  for (const char of text) {
    const value = VALUES.get(char);
    if (value === undefined) return null;
    buffer = ((buffer << 6) | value) & 0xffff;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[at++] = (buffer >> bits) & 0xff;
    }
  }
  // non-canonical: the unused low bits of the last character must be zero
  return (buffer & ((1 << bits) - 1)) === 0 ? out : null;
}

/** True for strict base64url text. */
export function isB64u(text: unknown): text is string {
  return typeof text === 'string' && decodeLoose(text) !== null;
}

/** Strict decoding; `length` additionally requires the exact decoded size. Violations are `header_invalid`. */
export function b64uDecode(text: unknown, length?: number): Uint8Array<ArrayBuffer> {
  const raw = typeof text === 'string' ? decodeLoose(text) : null;
  if (raw === null || (length !== undefined && raw.length !== length)) fail('header_invalid');
  return raw;
}
