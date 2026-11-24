// CON-02 (TypeScript side) in Node: every consumed vector file reproduced byte for byte, every negative vector
// refused with its code, and every vector file of contracts/vectors/ consumed by some implementation of src/core
// (the counterpart of `test_vectors_file_is_current`: a new vector file fails here until a consumer reads it).
import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../../helpers';
import {
  CONSUMED,
  Mismatch,
  negativeChecks,
  OPS,
  positiveChecks,
  refusal,
  runAll,
  same,
  stable,
  VECTOR_FILES,
} from './vector-suite';
import { LOG_CONSUMED } from '../log/vector-suite';

const VECTOR_DIR = join(WEB_DIR, '..', 'contracts', 'vectors');
// The membership-log and platform-log vectors (CRY-02) are consumed by src/core/log (tests/core/log/vector-suite.ts).
const LOG_AREA = /^(?:log|log-.+|platform-log)$/;

describe('contracts/vectors', () => {
  const onDisk = readdirSync(VECTOR_DIR)
    .filter((name) => name.endsWith('.json'))
    .map((name) => name.slice(0, -'.json'.length))
    .sort();

  it('loads every vector file of the repository', () => {
    expect(Object.keys(VECTOR_FILES).sort()).toEqual(onDisk);
  });

  it('consumes every vector file (crypto core and log core)', () => {
    expect(onDisk.filter((name) => !CONSUMED.includes(name) && !LOG_CONSUMED.includes(name))).toEqual([]);
    expect([...CONSUMED, ...LOG_CONSUMED].filter((name) => !onDisk.includes(name))).toEqual([]);
    expect(CONSUMED.filter((name) => LOG_AREA.test(name))).toEqual([]);
    expect(LOG_CONSUMED.filter((name) => !LOG_AREA.test(name))).toEqual([]);
  });

  it('knows every operation the consumed negative vectors name', () => {
    const named = new Set(CONSUMED.flatMap((name) => VECTOR_FILES[name].invalid.map((v: { op: string }) => v.op)));
    expect([...named].filter((op) => !Object.hasOwn(OPS, op))).toEqual([]);
    expect(Object.keys(OPS).filter((op) => !named.has(op))).toEqual([]);
  });

  it('has an about text and an invalid list in every consumed file', () => {
    for (const name of CONSUMED) {
      expect(typeof VECTOR_FILES[name].about).toBe('string');
      expect(Array.isArray(VECTOR_FILES[name].invalid)).toBe(true);
    }
  });

  for (const check of [...positiveChecks(), ...negativeChecks()]) it(check.name, check.run);
});

describe('vector suite helpers', () => {
  it('reports mismatches with both values', () => {
    expect(() => same({ a: 1 }, { a: 2 }, 'x')).toThrow(Mismatch);
    expect(() => same(new Uint8Array([1]), 'AQ', 'bytes')).not.toThrow();
    expect(stable({ b: [1, { d: 1, c: 2 }], a: null })).toBe('{"a":null,"b":[1,{"c":2,"d":1}]}');
  });

  it('returns null for an operation that succeeds and passes other errors on', async () => {
    expect(await refusal('canonical', { value: {} })).toBeNull();
    await expect(refusal('canonical', null)).rejects.toThrow(TypeError);
  });

  it('runs all checks for the browser and reports failures as text', async () => {
    const outcomes = await runAll();
    expect(outcomes.filter((outcome) => outcome.error !== null)).toEqual([]);
    expect(outcomes.length).toBe(positiveChecks().length + negativeChecks().length);
  });
});
