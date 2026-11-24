// U-WEB-lists-text (ADR-086 section 1): JSON pretty-printed before the clip, the one-line placeholder and the clip.
import { describe, expect, it } from 'vitest';
import { clip, CLIP_CHARS, LINE_CHARS, oneLine, prettyJson } from '../../../src/core/lists/text';

describe('list text', () => {
  it('pretty-prints JSON and gives the empty text for what JSON cannot express', () => {
    expect(prettyJson({ a: [1] })).toBe('{\n  "a": [\n    1\n  ]\n}');
    expect(prettyJson(undefined)).toBe('');
  });

  it('puts the start of a text on one line', () => {
    expect(oneLine('  a\n\n b\tc ')).toBe('a b c');
    const long = 'x'.repeat(LINE_CHARS + 5);
    expect(oneLine(long)).toBe(`${'x'.repeat(LINE_CHARS)}…`);
    // a megabyte costs no more than its start
    expect(oneLine('y'.repeat(1_000_000))).toHaveLength(LINE_CHARS + 1);
  });

  it('clips at the limit', () => {
    expect(CLIP_CHARS).toBe(20_000);
    expect(clip('abc', 2)).toBe('ab');
    expect(clip('abc', 3)).toBe('abc');
  });
});
