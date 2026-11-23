// Field checks shared by the formats of 04-crypto.md section 4, twin of `agent_hub.crypto.fields` and the
// `require_*` helpers of `agent_hub.crypto.encoding`. Every failure is `header_invalid` unless stated.
import { b64uDecode } from '../encoding/b64u';
import { canonical, isPlainObject, MAX_SAFE_INTEGER } from '../encoding/json';
import { isUuid7 } from '../encoding/ids';
import { parseTime } from '../encoding/time';
import { fail } from './errors';

export const KIND_RE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$/;
export const KIND_MAX = 64;

export type Fields = Record<string, unknown>;

/** `value` as a plain object with exactly the key set `keys`. */
export function requireKeys(value: unknown, keys: readonly string[]): Fields {
  if (!isPlainObject(value)) fail('header_invalid');
  const own = Object.keys(value);
  if (own.length !== keys.length || !keys.every((key) => Object.hasOwn(value, key))) fail('header_invalid');
  return value;
}

/** Exact key set (`header_invalid`), then printable ASCII and integer-only values (`canonical`). */
export function headerDict(value: unknown, keys: readonly string[]): Fields {
  const header = requireKeys(value, keys);
  canonical(header);
  return header;
}

/** An integer (not a boolean) within `minimum..maximum`. */
export function requireInt(value: unknown, minimum: number, maximum: number = MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum || value > maximum) {
    fail('header_invalid');
  }
  return value;
}

/** `raw` of exactly `length` bytes, else `code`. */
export function requireBytes(raw: Uint8Array, length: number, code: Parameters<typeof fail>[0] = 'header_invalid') {
  if (raw.length !== length) fail(code);
  return raw;
}

/** A UUIDv7 in lowercase text form. */
export function uuid(value: unknown): string {
  if (!isUuid7(value)) fail('header_invalid');
  return value;
}

/** A UUIDv7 or `""`. */
export function uuidOrEmpty(value: unknown): string {
  return value === '' ? '' : uuid(value);
}

/** A header time `YYYY-MM-DDTHH:MM:SS.mmmZ`. */
export function time(value: unknown): string {
  parseTime(value);
  return value as string;
}

/** b64u text of exactly `length` bytes. */
export function b64uBytes(value: unknown, length: number): string {
  b64uDecode(value, length);
  return value as string;
}

/** A dotted lowercase kind name such as `session.output`. */
export function kind(value: unknown): string {
  if (typeof value !== 'string' || value.length > KIND_MAX || !KIND_RE.test(value)) fail('header_invalid');
  return value;
}

/** Exactly `expected` (same type; `true` is not `1`). */
export function literal(value: unknown, expected: string | number): void {
  if (value !== expected) fail('header_invalid');
}

/** A non-empty string. */
export function text(value: unknown): string {
  if (typeof value !== 'string' || value === '') fail('header_invalid');
  return value;
}
