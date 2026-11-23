// Canonical JSON, body JSON and strict JSON parsing (04-crypto.md section 1), the twin of
// `agent_hub.crypto.encoding` (`canonical`, `body_json`, `parse_json`). Python's `json.dumps(sort_keys=True,
// separators=(",", ":"))` is reproduced exactly: object keys sorted by Unicode code point (not by UTF-16 code unit),
// the string escapes of `JSON.stringify` (identical to Python for well-formed text), and Python's float repr.
import { fail } from '../crypto/errors';
import { fromUtf8, utf8 } from './bytes';

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/** Integers in canonical JSON stay within the exact range of a JavaScript number. */
export const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;

/** Nesting limit of `parseJson` (structures of this protocol are shallow). */
export const MAX_DEPTH = 64;

const PRINTABLE = /^[\x20-\x7e]*$/;
const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

/** False for text with a lone surrogate (not valid Unicode, not encodable as UTF-8). */
export function isWellFormed(text: string): boolean {
  return !LONE_SURROGATE.test(text);
}

/** Only the characters 0x20-0x7E. */
export function isPrintableAscii(text: string): boolean {
  return PRINTABLE.test(text);
}

/** A plain object (JSON object): not null, not an array, prototype `Object.prototype` or `null`. */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null) return false;
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** Orders strings by Unicode code point, like Python's `sorted` on `str`. */
export function compareCodePoints(a: string, b: string): number {
  const left = [...a];
  const right = [...b];
  for (let index = 0; index < Math.min(left.length, right.length); index++) {
    const diff = left[index]!.codePointAt(0)! - right[index]!.codePointAt(0)!;
    if (diff !== 0) return diff;
  }
  return left.length - right.length;
}

function strings(value: unknown, out: string[]): string[] {
  if (typeof value === 'string') out.push(value);
  else if (Array.isArray(value)) value.forEach((item) => strings(item, out));
  else if (isPlainObject(value)) {
    for (const [key, item] of Object.entries(value)) {
      out.push(key);
      strings(item, out);
    }
  }
  return out;
}

/** Python's `repr(float)` for a finite non-integer number (shortest round-trip digits). */
function pythonFloat(value: number): string {
  const [mantissa, exponent] = value.toExponential().split('e') as [string, string];
  const exp = Number(exponent);
  // non-integers are below 2**53 < 1e16, so only the small exponents switch to scientific notation
  if (exp >= -4) return String(value);
  return `${mantissa}e-${String(-exp).padStart(2, '0')}`;
}

/** Serialises `value`; `integersOnly` refuses non-integer numbers. Unsupported values are `header_invalid`. */
function serialise(value: unknown, integersOnly: boolean): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string') {
    if (!isWellFormed(value)) fail('header_invalid');
    return JSON.stringify(value);
  }
  if (typeof value === 'number') {
    if (Number.isSafeInteger(value)) return String(value);
    if (integersOnly || !Number.isFinite(value) || Number.isInteger(value)) fail('header_invalid');
    return pythonFloat(value);
  }
  if (Array.isArray(value)) return `[${value.map((item) => serialise(item, integersOnly)).join(',')}]`;
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort(compareCodePoints);
    return `{${keys.map((key) => `${serialise(key, integersOnly)}:${serialise(value[key], integersOnly)}`).join(',')}}`;
  }
  return fail('header_invalid');
}

/**
 * Canonical JSON (section 1): sorted keys, `,` and `:`, printable ASCII only, integers only.
 * A string or key outside 0x20-0x7E is `non_ascii` (checked first, over the whole value); then a non-integer
 * number, an integer outside the safe range or any non-JSON value is `header_invalid`.
 */
export function canonical(value: unknown): Uint8Array<ArrayBuffer> {
  if (!strings(value, []).every(isPrintableAscii)) fail('non_ascii');
  return utf8(serialise(value, true));
}

/** Canonical JSON as text (always ASCII). */
export function canonicalText(value: unknown): string {
  return fromUtf8(canonical(value));
}

/**
 * Body JSON (section 1): UTF-8, sorted keys, no whitespace, non-ASCII kept; the plaintext of ciphertexts.
 * Lone surrogates, NaN/Infinity, integers outside the safe range and non-JSON values are `header_invalid`.
 */
export function bodyJson(value: unknown): Uint8Array<ArrayBuffer> {
  return utf8(serialise(value, false));
}

/** How `parseJson` treats numbers JavaScript cannot represent like Python does. */
export interface ParseOptions {
  /**
   * A literal with fraction or exponent and an integral value (`3.0`, `1e1`, `-0.0`) is a float in Python, which
   * canonical JSON refuses, but would be the integer 3 here. Default `'refuse'` (`header_invalid`), so that a header
   * re-encoded with `3.0` is refused like by the reference; `'number'` keeps the value (bodies that may carry floats).
   */
  readonly integralFloats?: 'refuse' | 'number';
  /** An integer literal beyond ±(2^53-1): default `'refuse'`; `'number'` keeps the inexact value (test data only). */
  readonly unsafeIntegers?: 'refuse' | 'number';
}

class Parser {
  private at = 0;

  constructor(
    private readonly text: string,
    private readonly options: ParseOptions,
  ) {}

  parse(): JsonValue {
    const value = this.value(0);
    this.space();
    if (this.at !== this.text.length) fail('header_invalid');
    return value;
  }

  private space(): void {
    while (' \t\n\r'.includes(this.text[this.at] ?? 'x')) this.at++;
  }

  private match(pattern: RegExp): string {
    pattern.lastIndex = this.at;
    const found = pattern.exec(this.text);
    if (found === null) fail('header_invalid');
    this.at += found[0].length;
    return found[0];
  }

  private value(depth: number): JsonValue {
    if (depth > MAX_DEPTH) fail('header_invalid');
    this.space();
    const char = this.text[this.at];
    if (char === '{') return this.object(depth);
    if (char === '[') return this.array(depth);
    if (char === '"') return this.string();
    const literal = this.match(/true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y);
    if (literal === 'true' || literal === 'false') return literal === 'true';
    if (literal === 'null') return null;
    const number = Number(literal);
    if (!Number.isFinite(number)) fail('header_invalid');
    const integerLiteral = /^-?[0-9]+$/.test(literal);
    // an integer literal beyond the safe range cannot be represented exactly
    if (integerLiteral && !Number.isSafeInteger(number) && this.options.unsafeIntegers !== 'number') {
      fail('header_invalid');
    }
    // a float literal with an integral value would silently become an integer
    if (!integerLiteral && Number.isInteger(number) && this.options.integralFloats !== 'number') fail('header_invalid');
    return number;
  }

  private string(): string {
    const token = this.match(/"(?:[^"\\]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"/y);
    // raw control characters are not allowed inside strings (escapes are)
    if ([...token].some((char) => char.charCodeAt(0) < 0x20)) fail('header_invalid');
    return JSON.parse(token) as string;
  }

  private array(depth: number): JsonValue[] {
    const items: JsonValue[] = [];
    this.at++;
    this.space();
    if (this.text[this.at] === ']') {
      this.at++;
      return items;
    }
    for (;;) {
      items.push(this.value(depth + 1));
      this.space();
      if (this.match(/[,\]]/y) === ']') return items;
    }
  }

  private object(depth: number): JsonObject {
    const result: JsonObject = {};
    this.at++;
    this.space();
    if (this.text[this.at] === '}') {
      this.at++;
      return result;
    }
    for (;;) {
      this.space();
      if (this.text[this.at] !== '"') fail('header_invalid');
      const key = this.string();
      if (Object.hasOwn(result, key)) fail('header_invalid');
      this.space();
      this.match(/:/y);
      // defineProperty: a key "__proto__" stays an own property
      Object.defineProperty(result, key, {
        value: this.value(depth + 1),
        enumerable: true,
        writable: true,
        configurable: true,
      });
      this.space();
      if (this.match(/[,}]/y) === '}') return result;
    }
  }
}

/**
 * Strict JSON of UTF-8 bytes (`parse_json`): invalid UTF-8, a byte order mark, repeated keys, NaN/Infinity, nesting
 * deeper than `MAX_DEPTH` and every syntax error are `header_invalid`; so are, unless `options` allow them, integer
 * literals beyond the safe range and float literals with an integral value (see `ParseOptions`).
 */
export function parseJson(raw: Uint8Array, options: ParseOptions = {}): JsonValue {
  let text: string | null;
  try {
    text = fromUtf8(raw);
  } catch {
    text = null;
  }
  if (text === null) fail('header_invalid');
  return new Parser(text, options).parse();
}
