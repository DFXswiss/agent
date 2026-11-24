// The UTF-8 text of a log page (ADR-078 section 3): a character that straddles two chunks belongs to the page it
// starts on; invalid bytes become U+FFFD.
import { describe, expect, it } from 'vitest';
import { utf8 } from '../../../src/core/encoding/bytes';
import { decodeWindow, missingBytes } from '../../../src/core/logs/text';

const bytes = (...values: number[]) => new Uint8Array(values);

describe('missingBytes', () => {
  it.each([
    ['nothing', bytes(), 0],
    ['ASCII', utf8('ab'), 0],
    ['a complete two-byte character', utf8('ä'), 0],
    ['the lead of a two-byte character', bytes(0x61, 0xc3), 1],
    ['two of three bytes', bytes(0xe2, 0x82), 1],
    ['the lead of a four-byte character', bytes(0xf0), 3],
    ['a complete four-byte character', utf8('😀'), 0],
    ['only continuation bytes', bytes(0x80, 0x80, 0x80, 0x80, 0x80), 0],
  ])('%s needs %d more', (_, value, need) => {
    expect(missingBytes(value)).toBe(need);
  });
});

describe('decodeWindow', () => {
  it('leaves out leading continuation bytes only when the page does not start the output', () => {
    expect(decodeWindow(bytes(0xa4, 0x62), true, null)).toBe('b');
    expect(decodeWindow(bytes(0xa4, 0x62), false, null)).toBe('�b');
    expect(decodeWindow(bytes(0x80, 0x80), true, null)).toBe('');
    expect(decodeWindow(bytes(0x80, 0x80, 0x80, 0x80), true, null)).toBe('�');
  });

  it('completes the last character from the next chunk', () => {
    expect(decodeWindow(bytes(0x61, 0xe2, 0x82), false, bytes(0xac, 0x62))).toBe('a€');
    expect(decodeWindow(bytes(0x61, 0xe2, 0x82), false, null)).toBe('a�');
    expect(decodeWindow(utf8('ok'), false, bytes(0x80))).toBe('ok');
    expect(decodeWindow(utf8('﻿x'), false, null)).toBe('﻿x');
  });
});
