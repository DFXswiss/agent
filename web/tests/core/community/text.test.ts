// Links in community texts and the sender mark (12-community.md section 6).
import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { segments } from '../../../src/core/community/links';
import { senderMark } from '../../../src/core/community/mark';

describe('segments', () => {
  it('turns http(s) URLs into links and keeps everything else as text', () => {
    expect(segments('')).toEqual([]);
    expect(segments('no links')).toEqual([{ kind: 'text', text: 'no links' }]);
    expect(segments('Siehe https://example.org/a?b=1 - danke.')).toEqual([
      { kind: 'text', text: 'Siehe ' },
      { kind: 'link', href: 'https://example.org/a?b=1' },
      { kind: 'text', text: ' - danke.' },
    ]);
    expect(segments('HTTP://X.ORG')).toEqual([{ kind: 'link', href: 'HTTP://X.ORG' }]);
  });

  it('leaves closing punctuation and unbalanced brackets outside the link', () => {
    expect(segments('(see http://a.org/x_(y)), ok')).toEqual([
      { kind: 'text', text: '(see ' },
      { kind: 'link', href: 'http://a.org/x_(y)' },
      { kind: 'text', text: '), ok' },
    ]);
    expect(segments('"https://a.org/?q=1!"')).toEqual([
      { kind: 'text', text: '"' },
      { kind: 'link', href: 'https://a.org/?q=1' },
      { kind: 'text', text: '!"' },
    ]);
    expect(segments('[http://a.org] {https://b.org}')).toEqual([
      { kind: 'text', text: '[' },
      { kind: 'link', href: 'http://a.org' },
      { kind: 'text', text: '] {' },
      { kind: 'link', href: 'https://b.org' },
      { kind: 'text', text: '}' },
    ]);
  });

  it('keeps invalid URLs, other schemes and markup as text', () => {
    expect(segments('x https://. y http://[ z')).toEqual([{ kind: 'text', text: 'x https://. y http://[ z' }]);
    expect(segments('javascript:alert(1) <b>https://a.org</b>')).toEqual([
      { kind: 'text', text: 'javascript:alert(1) <b>' },
      { kind: 'link', href: 'https://a.org' },
      { kind: 'text', text: '</b>' },
    ]);
  });
});

describe('senderMark', () => {
  it('is # and the first four base32 characters of SHA-256 of the user ID', async () => {
    const id = '01a1ee56-5e00-77be-8cf3-1f27bb0e92a2';
    const digest = createHash('sha256').update(id, 'utf8').digest();
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    const bits = [...digest.subarray(0, 3)].map((b) => b.toString(2).padStart(8, '0')).join('');
    const expected = '#' + [0, 5, 10, 15].map((i) => alphabet[parseInt(bits.slice(i, i + 5), 2)]).join('');
    expect(await senderMark(id)).toBe(expected);
    expect(await senderMark(id)).toMatch(/^#[A-Z2-7]{4}$/);
    expect(await senderMark('other')).not.toBe(expected);
  });
});
