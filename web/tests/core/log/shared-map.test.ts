// The shared maps of the log states (src/core/log/state.ts, KOR-35): `withEntry`/`withoutEntry` must read exactly
// like `new Map(map).set(key, value)` / `.delete(key)` – the same lookups, size and iteration order, across forks,
// without touching the map they were derived from – and `changedKeys` must name every key in which two maps differ.
import { describe, expect, it } from 'vitest';
import { changedKeys, changedPrincipals, sharedMap, withEntry, withoutEntry } from '../../../src/core/log/state';
import { actor, World } from './world';

function same<K, V>(actual: ReadonlyMap<K, V>, expected: ReadonlyMap<K, V>): void {
  expect([...actual]).toEqual([...expected]);
  expect([...actual.keys()]).toEqual([...expected.keys()]);
  expect([...actual.values()]).toEqual([...expected.values()]);
  expect([...actual.entries()]).toEqual([...expected.entries()]);
  expect(actual.size).toBe(expected.size);
  const seen: unknown[] = [];
  actual.forEach(function (this: unknown, value, key, map) {
    seen.push([key, value, this, map === actual]);
  }, 'this');
  expect(seen).toEqual([...expected].map(([key, value]) => [key, value, 'this', true]));
  for (const key of expected.keys()) {
    expect(actual.has(key)).toBe(true);
    expect(actual.get(key)).toBe(expected.get(key));
  }
}

/** Every key whose presence or value differs (the truth `changedKeys` must cover). */
function differing<K, V>(a: ReadonlyMap<K, V>, b: ReadonlyMap<K, V>): K[] {
  const keys = new Set([...a.keys(), ...b.keys()]);
  return [...keys].filter((key) => a.has(key) !== b.has(key) || a.get(key) !== b.get(key));
}

/** mulberry32 (test choices only). */
function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe('shared maps', () => {
  it('read like copied maps through random additions, replacements, deletions and forks', { timeout: 60_000 }, () => {
    const random = prng(35);
    let shared: ReadonlyMap<string, number> = sharedMap(new Map([['seed', 0]]));
    let copied = new Map(shared);
    const history: [ReadonlyMap<string, number>, Map<string, number>][] = [];
    let covered = 0;
    for (let i = 0; i < 2_000; i += 1) {
      const keys = [...copied.keys()];
      const choice = random();
      const known = keys[Math.floor(random() * keys.length)] ?? 'seed';
      const previous = shared;
      if (choice < 0.25) {
        shared = withoutEntry(shared, known);
        copied = new Map(copied);
        copied.delete(known);
      } else if (choice < 0.3) {
        // a key deleted before (or never there) is added again
        const key = `k${Math.floor(random() * i)}`;
        shared = withEntry(shared, key, i);
        copied = new Map(copied).set(key, i);
      } else {
        const key = choice < 0.55 ? known : `k${i}`;
        shared = withEntry(shared, key, i);
        copied = new Map(copied).set(key, i);
      }
      if (i % 16 === 0) same(shared, copied);
      const named = changedKeys(previous, shared);
      if (named !== null) {
        covered += 1;
        const all = new Set(named);
        expect(differing(previous, shared).every((key) => all.has(key))).toBe(true);
      }
      if (i % 97 === 0) history.push([shared, copied]);
    }
    same(shared, copied);
    expect(covered).toBe(2_000);
    // earlier maps are unchanged by later changes
    for (const [old, expected] of history) same(old, expected);
    expect(shared.has('missing')).toBe(false);
    expect(shared.get('missing')).toBeUndefined();
    expect(withoutEntry(shared, 'missing')).toBe(shared);
  });

  it('wrap any map without changing it and name the changes between relatives', () => {
    const base = new Map([
      ['a', 1],
      ['b', 2],
    ]);
    const next = withEntry(base, 'a', 3);
    same(next, new Map([...base, ['a', 3]]));
    expect([...base]).toEqual([
      ['a', 1],
      ['b', 2],
    ]);
    const deleted = withoutEntry(next, 'a');
    same(deleted, new Map([['b', 2]]));
    expect(deleted.get('a')).toBeUndefined();
    expect(deleted.has('a')).toBe(false);
    // a deleted key of the base added again moves to the end
    same(withEntry(deleted, 'a', 4), new Map([...[['b', 2] as const], ['a', 4]]));
    same(withoutEntry(withEntry(base, 'c', 5), 'c'), base);
    expect(changedKeys(base, base)).toEqual([]);
    // a plain map shares nothing known with the store built from a copy of it
    expect(changedKeys(base, next)).toBeNull();
    expect([...changedKeys(next, deleted)!]).toEqual(['a']);
    // a second child of one map is written into a copy (a store of its own): it reads alike, nothing is shared
    const second = withEntry(next, 'c', 5);
    same(second, new Map([...base, ['a', 3], ['c', 5]]));
    same(deleted, new Map([['b', 2]]));
    same(withoutEntry(next, 'b'), new Map([['a', 3]]));
    expect(changedKeys(second, deleted)).toBeNull();
    expect(changedKeys(withEntry(base, 'c', 5), deleted)).toBeNull();
    expect(changedKeys(base, new Map(base))).toBeNull();
    expect(changedKeys(next, new Map(base))).toBeNull();
    // the store copied the plain map: changing it later changes nothing derived from it (review KOR-35)
    const wrapped = sharedMap(base);
    expect(sharedMap(wrapped)).toBe(wrapped);
    base.set('b', 9);
    base.set('z', 9);
    same(
      wrapped,
      new Map([
        ['a', 1],
        ['b', 2],
      ]),
    );
    same(
      next,
      new Map([
        ['a', 3],
        ['b', 2],
      ]),
    );
  });

  it('answer deviceWgs from plain and shared assignment maps', async () => {
    const founder = await actor('founder');
    const world = await World.create(founder);
    const device = founder.id; // any ID: the rights only look at the maps
    const plain = world.state.with({
      principals: new Map([[device, { ...world.state.principals.get(device)!, kind: 'device', role: 'device' }]]),
      assignments: new Map([
        [`${device} w2`, 1],
        [`${device} w1`, 2],
        ['other w1', 3],
      ]),
    });
    expect(plain.deviceWgs(device)).toEqual(['w1', 'w2']);
    expect(plain.deviceWgs(device)).toEqual(['w1', 'w2']); // the index of the map is built once
    // maps that share nothing known: every principal may have changed
    expect(changedPrincipals(plain, plain.with({ access: new Map() }))).toBeNull();
    expect(changedPrincipals(plain, plain.with({ assignments: new Map() }))).toBeNull();
    expect([
      ...changedPrincipals(plain, plain.with({ assignments: withoutEntry(plain.assignments, 'other w1') }))!,
    ]).toEqual(['other']);
    expect(plain.deviceWgs('other')).toEqual([]);
    expect(plain.with({ assignments: new Map() }).deviceWgs(device)).toEqual([]);
    const shared = plain.with({
      assignments: withEntry(withoutEntry(plain.assignments, `${device} w2`), `${device} w0`, 4),
    });
    expect(shared.deviceWgs(device)).toEqual(['w0', 'w1']);
    const again = shared.with({
      assignments: withEntry(withoutEntry(shared.assignments, `${device} w0`), `${device} w0`, 5),
    });
    expect(again.deviceWgs(device)).toEqual(['w0', 'w1']);
    expect(plain.with({ assignments: withEntry(new Map(), 'other w1', 1) }).deviceWgs(device)).toEqual([]);
  });

  it('keep what a state answers when the caller changes its plain maps afterwards (review KOR-35)', async () => {
    const founder = await actor('founder');
    const world = await World.create(founder);
    const device = founder.id;
    const assignments = new Map([[`${device} w1`, 1]]);
    const state = world.state.with({
      principals: new Map([[device, { ...world.state.principals.get(device)!, kind: 'device', role: 'device' }]]),
      assignments,
    });
    expect(state.deviceWgs(device)).toEqual(['w1']);
    assignments.set(`${device} w2`, 2);
    assignments.delete(`${device} w1`);
    expect(state.deviceWgs(device)).toEqual(['w1']);
    expect(state.assignments.has(`${device} w2`)).toBe(false);
  });

  it('name the device of an organization assignment, and everyone for another org (review KOR-35)', async () => {
    const founder = await actor('founder');
    const world = await World.create(founder);
    const state = world.state;
    const assigned = state.with({ orgAssignments: withEntry(state.orgAssignments, 'dev', 7) });
    expect([...changedPrincipals(state, assigned)!]).toEqual(['dev']);
    const unassigned = assigned.with({ orgAssignments: withoutEntry(assigned.orgAssignments, 'dev') });
    expect([...changedPrincipals(assigned, unassigned)!]).toEqual(['dev']);
    expect(changedPrincipals(state, state.with({ orgAssignments: new Map() }))).toBeNull();
    expect(changedPrincipals(state, state.with({ org: 'other' }))).toBeNull();
    expect(changedPrincipals(state, state.with({ defaultWg: 'other' }))).toBeNull();
    expect([...changedPrincipals(state, state.with({ seq: 9 }))!]).toEqual([]);
  });

  it('answer the epoch holders read-only (review KOR-35)', async () => {
    const founder = await actor('founder');
    const world = await World.create(founder);
    const holders = world.log!.epochHolders('org', world.org, 1) as Set<string>;
    expect([...holders]).toEqual([founder.id]);
    expect(() => holders.add('x')).toThrow(TypeError);
    expect(() => holders.delete(founder.id)).toThrow(TypeError);
    expect(() => holders.clear()).toThrow(TypeError);
    expect(world.log!.epochHolders('org', world.org, 1)).toBe(holders);
    expect([...holders]).toEqual([founder.id]);
  });
});
