// U-WEB-crypto (title): the derivation of 06-web.md section 4.2 beyond contracts/tasks/title.json – the whitespace
// set is exactly the Unicode White_Space property.
import { describe, expect, it } from 'vitest';
import { deriveTitle, ELLIPSIS, TITLE_MAX_CODE_POINTS } from '../../../src/core/tasks/title';

const WHITE_SPACE = [
  ...Array.from({ length: 5 }, (_, i) => 0x09 + i),
  0x20,
  0x85,
  0xa0,
  0x1680,
  ...Array.from({ length: 11 }, (_, i) => 0x2000 + i),
  0x2028,
  0x2029,
  0x202f,
  0x205f,
  0x3000,
];

describe('deriveTitle', () => {
  it('collapses exactly the White_Space characters', () => {
    for (let point = 0; point <= 0x3000; point++) {
      const char = String.fromCodePoint(point);
      const expected = WHITE_SPACE.includes(point) ? 'a b' : `a${char}b`;
      if (!/[\r\n\v\f\u0085\u2028\u2029]/.test(char)) expect(deriveTitle(`a${char}b`)).toBe(expected);
    }
  });

  it('cuts at 80 code points with an ellipsis', () => {
    expect(deriveTitle('x'.repeat(TITLE_MAX_CODE_POINTS))).toHaveLength(80);
    expect(deriveTitle('x'.repeat(100))).toBe('x'.repeat(79) + ELLIPSIS);
    expect(deriveTitle('\n \n')).toBe('');
  });
});
