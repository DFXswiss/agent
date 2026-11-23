// The account audit of the "Security activity" list (`GET /v1/me/audit`).
import { describe, expect, it } from 'vitest';
import { loadAccountAudit } from '../../../src/core/agents/activity';
import { AGENT } from './world';

const row = {
  seq: 3,
  at: '2026-11-23T12:00:00.000Z',
  actor_id: null,
  actor_kind: 'user',
  action: 'agent.confirm',
  result: 'ok',
  org_id: null,
  wg_id: null,
  task_id: null,
  session_id: null,
  device_id: null,
  target_kind: 'agent',
  target_id: AGENT,
  detail: {},
  event_id: null,
  agent_id: AGENT,
};

describe('loadAccountAudit', () => {
  it('pages the account audit', async () => {
    const targets: string[] = [];
    const client = {
      request: async (_method: string, target: string) => (
        targets.push(target),
        { rows: [row, { junk: 1 }], next_before: 'c' }
      ),
    };
    expect(await loadAccountAudit(client)).toEqual({ rows: [row], next: 'c' });
    await loadAccountAudit(client, 'c');
    expect(targets).toEqual(['/v1/me/audit?limit=100', '/v1/me/audit?limit=100&before=c']);
  });
});
