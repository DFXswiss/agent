// U-WEB-crypto (encodings): strict b64u, canonical and body JSON, strict parsing, times, UUIDv7 and byte helpers –
// the edge cases beyond the shared vectors.
import { describe, expect, it } from 'vitest';
import { b64uDecode, b64uEncode, isB64u } from '../../../src/core/encoding/b64u';
import { bytesOf, concat, copy, equalBytes, fromHex, fromUtf8, toHex, utf8 } from '../../../src/core/encoding/bytes';
import { isUuid7, uuid7, uuid7Ms } from '../../../src/core/encoding/ids';
import {
  bodyJson,
  canonical,
  canonicalText,
  compareCodePoints,
  isPlainObject,
  isWellFormed,
  MAX_DEPTH,
  parseJson,
} from '../../../src/core/encoding/json';
import { formatSeconds, formatTime, parseSeconds, parseTime } from '../../../src/core/encoding/time';
import { CryptoError } from '../../../src/core/crypto/errors';

const code = (run: () => unknown): string => {
  try {
    run();
  } catch (error) {
    return (error as CryptoError).code;
  }
  return 'none';
};

describe('bytes', () => {
  it('converts and compares', () => {
    expect(toHex(fromHex('00ff10'))).toBe('00ff10');
    expect(fromHex('')).toEqual(new Uint8Array(0));
    expect(() => fromHex('abc')).toThrow(TypeError);
    expect(() => fromHex('zz')).toThrow(TypeError);
    expect(concat(new Uint8Array([1]), new Uint8Array([2, 3]))).toEqual(new Uint8Array([1, 2, 3]));
    expect(equalBytes(new Uint8Array([1, 2]), new Uint8Array([1, 2]))).toBe(true);
    expect(equalBytes(new Uint8Array([1, 2]), new Uint8Array([1, 3]))).toBe(false);
    expect(equalBytes(new Uint8Array([1]), new Uint8Array([1, 2]))).toBe(false);
    expect(fromUtf8(utf8('Grüsse 👋'))).toBe('Grüsse 👋');
    expect(() => fromUtf8(new Uint8Array([0xff]))).toThrow();
    expect(fromUtf8(new Uint8Array([0xef, 0xbb, 0xbf, 0x41]))).toBe('\ufeffA');
  });

  it('copies views and buffers into own buffers', () => {
    const buffer = new Uint8Array([1, 2, 3, 4]).buffer;
    const view = new Uint8Array(buffer, 1, 2);
    const copied = copy(view);
    copied[0] = 9;
    expect(view[0]).toBe(2);
    expect(bytesOf(view)).toEqual(new Uint8Array([2, 3]));
    expect(bytesOf(buffer)).toEqual(new Uint8Array([1, 2, 3, 4]));
    expect(bytesOf(new DataView(buffer, 2))).toEqual(new Uint8Array([3, 4]));
  });
});

describe('b64u', () => {
  it('round-trips every length', () => {
    for (let n = 0; n < 40; n++) {
      const raw = Uint8Array.from({ length: n }, (_, i) => (i * 37 + n) & 0xff);
      expect(b64uDecode(b64uEncode(raw))).toEqual(raw);
    }
  });

  it('refuses everything that is not strict', () => {
    for (const text of ['Zg==', 'Zh', 'Zm9vY', '+/', 'Zm9v ', 'Zm9v\n', 'Zé', null, 3]) {
      expect(isB64u(text)).toBe(false);
      expect(code(() => b64uDecode(text))).toBe('header_invalid');
    }
    expect(code(() => b64uDecode('Zm9v', 4))).toBe('header_invalid');
    expect(b64uDecode('Zm9v', 3)).toEqual(utf8('foo'));
    expect(isB64u('')).toBe(true);
  });
});

describe('canonical JSON', () => {
  it('sorts keys and checks every string first', () => {
    expect(canonicalText({ b: 1, a: [true, null, 'x'] })).toBe('{"a":[true,null,"x"],"b":1}');
    expect(code(() => canonical({ a: 1.5, b: 'é' }))).toBe('non_ascii');
    expect(code(() => canonical(['ok', { 'k\u00e9': 1 }]))).toBe('non_ascii');
  });

  it('refuses every non-JSON or non-integer value', () => {
    for (const value of [
      1.5,
      2 ** 53,
      NaN,
      Infinity,
      undefined,
      () => 1,
      1n,
      new Date(0),
      new Uint8Array(1),
      { a: undefined },
    ]) {
      expect(code(() => canonical(value))).toBe('header_invalid');
    }
    expect(canonicalText(Object.assign(Object.create(null), { z: 0, a: -0 }))).toBe('{"a":0,"z":0}');
  });
});

describe('body JSON', () => {
  it('matches Python json.dumps(ensure_ascii=False, sort_keys=True)', () => {
    expect(fromUtf8(bodyJson({ b: false, a: 'ü\u2028"\\' }))).toBe('{"a":"ü\u2028\\"\\\\","b":false}');
    // Python's float repr: shortest digits, scientific below 1e-4
    const floats = [0.5, -2.25, 0.0001, 0.00001, 1.5e-7, 123456.789, 1e-300, 5e-324];
    expect(floats.map((value) => fromUtf8(bodyJson(value)))).toEqual([
      '0.5',
      '-2.25',
      '0.0001',
      '1e-05',
      '1.5e-07',
      '123456.789',
      '1e-300',
      '5e-324',
    ]);
  });

  it('refuses values Python would not write', () => {
    for (const value of ['\ud800', { '\udc00': 1 }, NaN, -Infinity, 2 ** 53, 1e300, Symbol('x')]) {
      expect(code(() => bodyJson(value))).toBe('header_invalid');
    }
  });

  it('sorts by code point, not by UTF-16 code unit', () => {
    expect(compareCodePoints('\uffff', '\u{1f600}')).toBeLessThan(0);
    expect('\uffff' < '\u{1f600}').toBe(false);
    expect(compareCodePoints('ab', 'a')).toBeGreaterThan(0);
    expect(compareCodePoints('a', 'a')).toBe(0);
  });

  it('knows plain objects and well-formed text', () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject(Object.create(null))).toBe(true);
    expect(isPlainObject([])).toBe(false);
    expect(isPlainObject(null)).toBe(false);
    expect(isPlainObject(new Map())).toBe(false);
    expect(isWellFormed('a\u{1f600}')).toBe(true);
    expect(isWellFormed('\udc00\ud800')).toBe(false);
  });
});

describe('parseJson', () => {
  const parse = (text: string) => parseJson(utf8(text));

  it('parses JSON like Python json.loads', () => {
    expect(parse(' {"a": [1, -2.5e-3, true, false, null, "x\\u00e9\\n"], "b": {}, "c": []} ')).toEqual({
      a: [1, -0.0025, true, false, null, 'xé\n'],
      b: {},
      c: [],
    });
    const proto = parse('{"__proto__": {"polluted": 1}}') as Record<string, unknown>;
    expect(Object.hasOwn(proto, '__proto__')).toBe(true);
    expect(Object.getPrototypeOf(proto)).toBe(Object.prototype);
    expect(parse('"\\ud800"')).toBe('\ud800');
    expect(parse('9007199254740991')).toBe(2 ** 53 - 1);
  });

  it('refuses everything else with header_invalid', () => {
    const bad = [
      '',
      '{"a":1,"a":2}',
      'NaN',
      'Infinity',
      '[1,]',
      '{"a":1,}',
      '{a:1}',
      '01',
      '1.',
      '"\u0001"',
      '"\\x"',
      '[1 2]',
      '{"a" 1}',
      '9007199254740992',
      '1e400',
      'true false',
      '{"a":1} x',
      '[',
      '{',
      '{"a":',
    ];
    for (const text of bad) expect(code(() => parse(text))).toBe('header_invalid');
    expect(code(() => parseJson(new Uint8Array([0xef, 0xbb, 0xbf, 0x31])))).toBe('header_invalid');
    expect(code(() => parseJson(new Uint8Array([0x22, 0xff, 0x22])))).toBe('header_invalid');
  });

  it('refuses float literals with integral values unless allowed, like Python canonical JSON', () => {
    for (const text of ['3.0', '1e1', '-0.0', '{"epoch":3.0}', '2.5e3'])
      expect(code(() => parse(text))).toBe('header_invalid');
    expect(parseJson(utf8('[3.0,1e1]'), { integralFloats: 'number' })).toEqual([3, 10]);
    expect(code(() => canonical(parseJson(utf8('{"epoch":3.5}'))))).toBe('header_invalid');
    expect(parseJson(utf8('9007199254740993'), { unsafeIntegers: 'number' })).toBe(2 ** 53);
  });

  it('limits the nesting depth', () => {
    expect(parse('['.repeat(MAX_DEPTH + 1) + ']'.repeat(MAX_DEPTH + 1))).toBeInstanceOf(Array);
    expect(code(() => parse('['.repeat(MAX_DEPTH + 2) + ']'.repeat(MAX_DEPTH + 2)))).toBe('header_invalid');
  });
});

describe('times', () => {
  it('formats and parses header times', () => {
    expect(formatTime(0)).toBe('1970-01-01T00:00:00.000Z');
    expect(parseTime('2024-02-29T23:59:59.999Z')).toBe(Date.UTC(2024, 1, 29, 23, 59, 59, 999));
    expect(parseTime('0001-01-01T00:00:00.000Z')).toBe(-62135596800000);
    expect(formatTime(-62135596800000)).toBe('0001-01-01T00:00:00.000Z');
    expect(formatTime(253402300799999)).toBe('9999-12-31T23:59:59.999Z');
  });

  it('refuses impossible dates and other forms', () => {
    const bad = [
      '2025-02-29T00:00:00.000Z',
      '2026-13-01T00:00:00.000Z',
      '2026-01-01T24:00:00.000Z',
      '2026-01-01T00:60:00.000Z',
      '2026-01-01T00:00:60.000Z',
      '0000-01-01T00:00:00.000Z',
      '2026-01-01T00:00:00.000z',
      '2026-01-01 00:00:00.000Z',
      42,
    ];
    for (const text of bad) expect(code(() => parseTime(text))).toBe('header_invalid');
    for (const ms of [1.5, -62135596800001, 253402300800000, NaN])
      expect(code(() => formatTime(ms))).toBe('header_invalid');
  });

  it('formats and parses second-precision times', () => {
    expect(formatSeconds(1795003200)).toBe('2026-11-18T12:00:00Z');
    expect(parseSeconds('2026-11-18T12:00:00Z')).toBe(1795003200);
    expect(code(() => formatSeconds(1.5))).toBe('header_invalid');
    expect(code(() => parseSeconds('2026-11-18T09:20:00.000Z'))).toBe('header_invalid');
  });
});

describe('UUIDv7', () => {
  it('creates random and deterministic IDs', () => {
    const id = uuid7();
    expect(isUuid7(id)).toBe(true);
    expect(Math.abs(uuid7Ms(id) - Date.now())).toBeLessThan(60_000);
    expect(uuid7(2 ** 48 - 1, new Uint8Array(10))).toBe('ffffffff-ffff-7000-8000-000000000000');
    expect(uuid7Ms('ffffffff-ffff-7000-8000-000000000000')).toBe(2 ** 48 - 1);
  });

  it('refuses invalid inputs', () => {
    for (const [ms, random] of [
      [-1, new Uint8Array(10)],
      [2 ** 48, new Uint8Array(10)],
      [1.5, new Uint8Array(10)],
      [0, new Uint8Array(9)],
    ] as const) {
      expect(code(() => uuid7(ms, random))).toBe('header_invalid');
    }
    for (const value of ['0193005a-0000-4000-8000-00000000005a', 'X', 7]) {
      expect(isUuid7(value)).toBe(false);
      expect(code(() => uuid7Ms(value))).toBe('header_invalid');
    }
  });
});
