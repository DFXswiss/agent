// U-WEB-core: the runtime of a session or task (KOR-40).
import { describe, expect, it } from 'vitest';
import { runtimeSeconds } from '../../../src/core/sessions/runtime';

const START = '2026-11-21T12:00:00.000Z';
const NOW = Date.parse('2026-11-21T13:02:03.900Z');

describe('runtimeSeconds', () => {
  it('runs to now while running and to the end once ended', () => {
    expect(runtimeSeconds(START, null, true, NOW)).toBe(3723);
    expect(runtimeSeconds(START, undefined, true, NOW)).toBe(3723);
    expect(runtimeSeconds(START, '2026-11-21T12:00:45.500Z', false, NOW)).toBe(45);
  });

  it('knows nothing of an ended object without its end, or of malformed times', () => {
    expect(runtimeSeconds(START, null, false, NOW)).toBeNull();
    expect(runtimeSeconds('garbage', null, true, NOW)).toBeNull();
    expect(runtimeSeconds(START, 'garbage', false, NOW)).toBeNull();
  });

  it('never goes below zero (a device clock ahead of the browser)', () => {
    expect(runtimeSeconds('2026-11-21T14:00:00.000Z', null, true, NOW)).toBe(0);
  });
});
