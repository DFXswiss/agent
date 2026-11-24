// U-WEB-credentials (KOR-50, ADR-081 section 5): the newest model switch of a task from the audit rows
// `session.model_switched`; rows of another action or with a malformed detail are left out.
import { describe, expect, it } from 'vitest';
import { loadModelSwitch, modelSwitch, SWITCH_ACTION } from '../../../src/core/credentials/switches';
import { SESSION, switchRow, TASK } from './switch-rows';
import { credWorld } from './world';

describe('model switches', () => {
  it('reads the switch of a row', () => {
    expect(modelSwitch(switchRow(1))).toEqual({
      at: Date.parse('2026-11-24T13:05:00.000Z'),
      sessionId: SESSION,
      fromAccess: 'subscription',
      toAccess: 'platform',
      fromModel: null,
      toModel: 'deepseek/flash',
      cause: 'exhausted',
      until: Date.parse('2026-11-24T14:05:00.000Z'),
    });
    expect(modelSwitch(switchRow(1, { until: null, cause: 'released' }))).toMatchObject({ until: null });
    for (const broken of [
      switchRow(1, {}, 'session.failed'),
      switchRow(1, { from_access: '' }),
      switchRow(1, { to_access: 3 }),
      switchRow(1, { cause: null }),
      switchRow(1, { from_model: 5 }),
      switchRow(1, { to_model: '' }),
      switchRow(1, { until: 'soon' }),
      switchRow(1, { until: 5 }),
    ]) {
      expect(modelSwitch(broken)).toBeNull();
    }
  });

  it('loads the newest switch of the task from the audit, or none', async () => {
    const t = await credWorld();
    let rows: unknown[] = [];
    t.client.on(`GET /v1/orgs/${t.org}/audit`, () => ({ rows, next_before: null }));
    expect(await loadModelSwitch(t.context, t.org, TASK)).toBeNull();
    const target = new URL(t.client.of(`GET /v1/orgs/${t.org}/audit`)[0]!.target, 'https://hub.test');
    expect(target.searchParams.get('action')).toBe(SWITCH_ACTION);
    expect(target.searchParams.get('task')).toBe(TASK);
    rows = [switchRow(3, { cause: 3 }), switchRow(2, { to_access: 'api_key' }), switchRow(1)];
    expect(await loadModelSwitch(t.context, t.org, TASK)).toMatchObject({ toAccess: 'api_key' });
  });
});
