import { describe, expect, it } from 'vitest';
import { Cursors, MAX_QUERY_CURSORS } from '../../../src/core/net/cursors';

describe('cursors', () => {
  it('classifies frames and only moves forward', () => {
    const cursors = new Cursors({ a: 3 });
    expect(
      ['a', 'b'].map((s) => [
        cursors.position(s, 1),
        cursors.position(s, 3),
        cursors.position(s, 4),
        cursors.position(s, 6),
      ]),
    ).toEqual([
      ['duplicate', 'duplicate', 'next', 'gap'],
      ['next', 'gap', 'gap', 'gap'],
    ]);
    expect(cursors.advance('a', 2)).toBe(false);
    expect(cursors.advance('a', 5)).toBe(true);
    expect(cursors.advance('b', 0)).toBe(true);
    expect(cursors.get('a')).toBe(5);
    expect(cursors.has('b')).toBe(true);
    expect(cursors.get('c')).toBeUndefined();
  });

  it('notifies listeners of changes and builds the query', () => {
    const cursors = new Cursors();
    const seen: unknown[] = [];
    const off = cursors.onChange((snapshot) => seen.push(snapshot));
    cursors.advance('b', 2);
    cursors.advance('a', 1);
    cursors.forget('c');
    cursors.forget('b');
    off();
    cursors.advance('c', 1);
    expect(seen).toEqual([{ b: 2 }, { a: 1, b: 2 }, { a: 1 }]);
    cursors.reset('a', 1);
    cursors.reset('a', 0);
    expect(cursors.get('a')).toBe(0);
    cursors.reset('a', 1);
    expect(cursors.scopes()).toEqual(['a', 'c']);
    expect(cursors.query()).toBe('a:1,c:1');
    const many = new Cursors(
      Object.fromEntries(Array.from({ length: 300 }, (_, i) => [`s${String(i).padStart(3, '0')}`, i])),
    );
    expect(many.query().split(',')).toHaveLength(MAX_QUERY_CURSORS);
    expect(new Cursors().query()).toBe('');
  });
});
