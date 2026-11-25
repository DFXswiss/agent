// U-WEB-core-session-failover (KOR-58, ADR-086 sections 2, 3 and 6): the interruption of a session by a device failure
// and the continuation of a lost session, tolerant of hubs that do not send `interrupted_at` and `failover_of`.
import { describe, expect, it } from 'vitest';
import { deviceFailure } from '../../../src/core/sessions/failover';
import type { SessionEntry } from '../../../src/core/store/slices/tasks';

const LOST = '0190f000-0000-7000-8000-0000000000a1';
const SINCE = '2026-11-25T05:40:00.000Z';

const entry = (fields: Record<string, unknown>): SessionEntry =>
  ({
    row: { session_id: 's', state: 'working', ...fields },
    transitions: [],
    estimatedAt: null,
  }) as unknown as SessionEntry;

describe('deviceFailure', () => {
  it('reports nothing without a row or without the fields (older hub)', () => {
    expect(deviceFailure(null)).toBeNull();
    expect(deviceFailure(undefined)).toBeNull();
    expect(deviceFailure({ row: null } as unknown as SessionEntry)).toBeNull();
    expect(deviceFailure(entry({}))).toBeNull();
    expect(deviceFailure(entry({ interrupted_at: null, failover_of: null }))).toBeNull();
  });

  it('reports an interrupted session with the time its device was lost', () => {
    expect(deviceFailure(entry({ interrupted_at: SINCE }))).toEqual({
      interruptedAt: Date.parse(SINCE),
      failoverOf: null,
    });
    expect(deviceFailure(entry({ state: 'agent_starting', interrupted_at: SINCE }))?.interruptedAt).toBe(
      Date.parse(SINCE),
    );
  });

  it('no longer reports an ended session as interrupted', () => {
    for (const state of ['stopped', 'failed'])
      expect(deviceFailure(entry({ state, interrupted_at: SINCE }))).toBeNull();
  });

  it('ignores a malformed time or session ID', () => {
    for (const value of ['yesterday', 7, '2026-11-25T05:40:00Z'])
      expect(deviceFailure(entry({ interrupted_at: value }))).toBeNull();
    for (const value of ['session', 7, LOST.toUpperCase()])
      expect(deviceFailure(entry({ failover_of: value }))).toBeNull();
  });

  it('reports the lost session a session continues, also while it is interrupted itself', () => {
    expect(deviceFailure(entry({ failover_of: LOST }))).toEqual({ interruptedAt: null, failoverOf: LOST });
    expect(deviceFailure(entry({ state: 'stopped', interrupted_at: SINCE, failover_of: LOST }))).toEqual({
      interruptedAt: null,
      failoverOf: LOST,
    });
    expect(deviceFailure(entry({ interrupted_at: SINCE, failover_of: LOST }))).toEqual({
      interruptedAt: Date.parse(SINCE),
      failoverOf: LOST,
    });
  });
});
