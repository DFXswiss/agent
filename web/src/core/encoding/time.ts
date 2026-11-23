// Time formats (04-crypto.md section 1): header times `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC, milliseconds) and the
// second-precision `YYYY-MM-DDTHH:MM:SSZ` of `agent-request-v1` and the `agent-session-v1` statement.
import { fail } from '../crypto/errors';

const HEADER_TIME = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z$/;
const SECONDS_TIME = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$/;

/** Unix milliseconds of a valid calendar time (years 1-9999, no leap second), else null. */
function utcMs(parts: readonly number[]): number | null {
  const [year, month, day, hour, minute, second, ms] = parts as [
    number,
    number,
    number,
    number,
    number,
    number,
    number,
  ];
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, ms);
  const valid =
    year >= 1 &&
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day &&
    date.getUTCHours() === hour &&
    date.getUTCMinutes() === minute &&
    date.getUTCSeconds() === second;
  return valid ? date.getTime() : null;
}

function parseWith(pattern: RegExp, text: unknown): number {
  const match = typeof text === 'string' ? pattern.exec(text) : null;
  const ms = match === null ? null : utcMs([...match.slice(1).map(Number), 0, 0].slice(0, 7));
  if (ms === null) fail('header_invalid');
  return ms;
}

/** 0001-01-01T00:00:00.000Z and 9999-12-31T23:59:59.999Z, the range of the four-digit year form. */
const MIN_MS = -62135596800000;
const MAX_MS = 253402300799999;

function iso(ms: number): string {
  if (!Number.isSafeInteger(ms) || ms < MIN_MS || ms > MAX_MS) fail('header_invalid');
  return new Date(ms).toISOString();
}

/** `YYYY-MM-DDTHH:MM:SS.mmmZ` of Unix milliseconds (header times). */
export function formatTime(unixMs: number): string {
  return iso(unixMs);
}

/** Unix milliseconds of a header time; any other form or an impossible date is `header_invalid`. */
export function parseTime(text: unknown): number {
  return parseWith(HEADER_TIME, text);
}

/** `YYYY-MM-DDTHH:MM:SSZ` of whole Unix seconds (`agent-request-v1`, `agent-session-v1`). */
export function formatSeconds(unixSeconds: number): string {
  if (!Number.isSafeInteger(unixSeconds)) fail('header_invalid');
  return iso(unixSeconds * 1000).replace(/\.000Z$/, 'Z');
}

/** Whole Unix seconds of a `YYYY-MM-DDTHH:MM:SSZ` text (`header_invalid` otherwise). */
export function parseSeconds(text: unknown): number {
  return parseWith(SECONDS_TIME, text) / 1000;
}
