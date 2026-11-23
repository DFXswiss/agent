import { describe, expect, it } from 'vitest';
import { baseName, collect, hasShape, isNamed, isRecord, ordered } from '../../src/core/registry';

const isNumber = (value: unknown): value is number => typeof value === 'number';

describe('collect', () => {
  it('returns the named export of every module sorted by path', () => {
    const modules = { './b.ts': { item: 2 }, './a.ts': { item: 1 } };
    expect(collect(modules, 'item', isNumber)).toEqual([
      { path: './a.ts', value: 1 },
      { path: './b.ts', value: 2 },
    ]);
  });

  it('returns nothing for an empty glob', () => {
    expect(collect({}, 'item', isNumber)).toEqual([]);
  });

  it.each([
    ['a missing export', { other: 1 }],
    ['an invalid export', { item: 'x' }],
    ['a module that is not an object', 'text'],
    ['a null module', null],
  ])('refuses %s', (_label, namespace) => {
    expect(() => collect({ './x.ts': namespace }, 'item', isNumber)).toThrow(
      './x.ts: missing or invalid export "item"',
    );
  });
});

describe('ordered', () => {
  it('sorts by order, then id', () => {
    const entries = [
      { id: 'c', order: 2 },
      { id: 'b', order: 1 },
      { id: 'a', order: 1 },
    ];
    expect(ordered(entries).map((entry) => entry.id)).toEqual(['a', 'b', 'c']);
  });

  it('refuses duplicate ids', () => {
    expect(() =>
      ordered([
        { id: 'a', order: 1 },
        { id: 'a', order: 2 },
      ]),
    ).toThrow('duplicate registration "a"');
  });
});

describe('helpers', () => {
  it('derives the base name', () => {
    expect(baseName('./chat-items/session.output.tsx', '.tsx')).toBe('session.output');
    expect(baseName('plain', '.tsx')).toBe('plain');
  });

  it('recognises records and named values', () => {
    expect(isRecord({})).toBe(true);
    expect(isRecord([])).toBe(false);
    expect(isRecord(null)).toBe(false);
    expect(isRecord('x')).toBe(false);
    expect(isNamed({ name: 'x' })).toBe(true);
    expect(isNamed({ name: '' })).toBe(false);
    expect(isNamed({ name: 1 })).toBe(false);
    expect(isNamed(null)).toBe(false);
  });

  it('checks shapes including components', () => {
    const shape = { id: 'string', order: 'number', Component: 'component' } as const;
    expect(hasShape({ id: 'a', order: 1, Component: () => null }, shape)).toBe(true);
    expect(hasShape({ id: 'a', order: 1, Component: { $$typeof: Symbol.for('react.memo') } }, shape)).toBe(true);
    expect(hasShape({ id: 'a', order: 1, Component: {} }, shape)).toBe(false);
    expect(hasShape({ id: 'a', order: 1, Component: 'div' }, shape)).toBe(false);
    expect(hasShape({ id: 'a', order: '1', Component: () => null }, shape)).toBe(false);
    expect(hasShape('a', shape)).toBe(false);
  });
});
