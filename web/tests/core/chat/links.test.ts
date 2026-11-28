// U-WEB-core-chat-links: bare URLs of plain texts (prompts, fallbacks, operation cards) as link parts under the link
// rules of `safeUrl` (06-web.md section 2.1).
import { describe, expect, it } from 'vitest';
import { linkParts, trimUrl } from '../../../src/core/chat/links';

describe('linkParts', () => {
  it('keeps text without URLs as one part', () => {
    expect(linkParts('')).toEqual([]);
    expect(linkParts('no links')).toEqual([{ kind: 'text', text: 'no links' }]);
  });

  it('links http, https and mailto URLs without the closing punctuation', () => {
    expect(
      linkParts('SEC-020: https://github.com/o/r/pull/6371, http://a.example/x_(y). Mail MAILTO:a@b.example!'),
    ).toEqual([
      { kind: 'text', text: 'SEC-020: ' },
      { kind: 'link', href: 'https://github.com/o/r/pull/6371' },
      { kind: 'text', text: ', ' },
      { kind: 'link', href: 'http://a.example/x_(y)' },
      { kind: 'text', text: '. Mail ' },
      { kind: 'link', href: 'MAILTO:a@b.example' },
      { kind: 'text', text: '!' },
    ]);
    expect(linkParts('<https://a.example>"https://b.example"')).toEqual([
      { kind: 'text', text: '<' },
      { kind: 'link', href: 'https://a.example' },
      { kind: 'text', text: '>"' },
      { kind: 'link', href: 'https://b.example' },
      { kind: 'text', text: '"' },
    ]);
  });

  it('keeps other schemes, empty and unparsable URLs as text', () => {
    const text = 'javascript:alert(1) data:text/html,x vbscript:x file:///etc mailto:. https://. http://[ ftp://x';
    expect(linkParts(text)).toEqual([{ kind: 'text', text }]);
  });

  it('links only the given schemes', () => {
    expect(linkParts('http://a.example https://b.example mailto:c@d.example', new Set(['https:']))).toEqual([
      { kind: 'text', text: 'http://a.example ' },
      { kind: 'link', href: 'https://b.example' },
      { kind: 'text', text: ' mailto:c@d.example' },
    ]);
  });

  it('trims closing punctuation and unbalanced brackets', () => {
    expect(trimUrl('https://a.example/x).')).toBe('https://a.example/x');
    expect(trimUrl('https://a.example/(x)]')).toBe('https://a.example/(x)');
    expect(trimUrl('https://a.example/?q=1!"')).toBe('https://a.example/?q=1');
  });
});
