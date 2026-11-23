// CON-02 (log part, TypeScript side) and CON-03: every log vector file is consumed, every negative log vector is
// refused with exactly its code, the positive parts (heads, projections, R, batches, wraps, catch-up plans, events)
// are reproduced, and the event kind matrix equals contracts/enums/event-kinds.json.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { LOG_CODES } from '../../../src/core/crypto/errors';
import { matrix } from '../../../src/core/log/events';
import { WEB_DIR } from '../../helpers';
import { VECTOR_FILES } from '../crypto/vector-suite';
import { LOG_CONSUMED, LogContext, logNegativeChecks, logPositiveChecks } from './vector-suite';

const CONTRACTS = join(WEB_DIR, '..', 'contracts');
const LOG_FILE = /^(?:log|log-.+|platform-log)\.json$/;

describe('CON-02 log vectors (TypeScript)', () => {
  it('consumes every log vector file of contracts/vectors (counted)', () => {
    const onDisk = readdirSync(join(CONTRACTS, 'vectors'))
      .filter((name) => LOG_FILE.test(name))
      .map((name) => name.slice(0, -'.json'.length))
      .sort();
    expect(onDisk).toEqual([...LOG_CONSUMED].sort());
    expect(onDisk.length).toBe(5);
  });

  it('knows every operation the log vectors name', () => {
    const ctx = new LogContext({});
    const named = new Set(LOG_CONSUMED.flatMap((name) => VECTOR_FILES[name].invalid.map((v: { op: string }) => v.op)));
    expect([...named].filter((op) => !Object.hasOwn(ctx.ops, op))).toEqual([]);
  });

  it('has a negative vector for every log code and one broken variant per rule L1-L10 and P1-P4', () => {
    const cases = LOG_CONSUMED.flatMap((name) => VECTOR_FILES[name].invalid as { name: string; error: string }[]);
    const codes = new Set(cases.map((c) => c.error));
    expect(LOG_CODES.filter((code) => code !== 'seq_conflict' && !codes.has(code))).toEqual([]);
    expect(codes.has('seq_conflict')).toBe(true);
    const logNames = (VECTOR_FILES.log.invalid as { name: string }[]).map((c) => c.name);
    for (const rule of ['L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7', 'L8', 'L9', 'L10']) {
      expect(logNames.some((name) => name.startsWith(`${rule}_`))).toBe(true);
    }
    const platformNames = (VECTOR_FILES['platform-log'].invalid as { name: string }[]).map((c) => c.name);
    for (const rule of ['P1', 'P2', 'P3', 'P4']) {
      expect(platformNames.some((name) => name.startsWith(`${rule}_`))).toBe(true);
    }
  });

  for (const check of [...logPositiveChecks(), ...logNegativeChecks()]) it(check.name, check.run);
});

describe('CON-03 event kind matrix (TypeScript)', () => {
  it('equals contracts/enums/event-kinds.json without its description', () => {
    const contract = JSON.parse(readFileSync(join(CONTRACTS, 'enums', 'event-kinds.json'), 'utf8'));
    delete contract.description;
    expect(matrix()).toEqual(contract);
    expect(VECTOR_FILES['log-events'].matrix).toEqual(contract);
  });
});

describe('log vector context', () => {
  it('returns null for an operation that succeeds and passes other errors on', async () => {
    const ctx = new LogContext({});
    expect(await ctx.refusal('check_replay', { stored: null, incoming: {} })).toBeNull();
    await expect(ctx.refusal('verify_log', { log: { name: 'missing', count: 0 } })).rejects.toThrow(TypeError);
  });
});
