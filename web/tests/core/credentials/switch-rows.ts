// Audit rows `session.model_switched` for the tests of the model switch line (KOR-50).
import { SWITCH_ACTION } from '../../../src/core/credentials/switches';
import type { AuditRow } from '../../../src/core/monitoring/audit';

export const SESSION = '0190f000-0000-7000-8000-0000000000e1';
export const TASK = '0190f000-0000-7000-8000-0000000000f1';

export function switchRow(seq: number, detail: Record<string, unknown> = {}, action = SWITCH_ACTION): AuditRow {
  return {
    seq,
    at: '2026-11-24T13:05:00.000Z',
    actor_id: null,
    actor_kind: 'device',
    action,
    result: 'ok',
    org_id: null,
    wg_id: null,
    task_id: TASK,
    session_id: SESSION,
    device_id: null,
    target_kind: null,
    target_id: null,
    detail: {
      from_access: 'subscription',
      to_access: 'platform',
      from_model: null,
      to_model: 'deepseek/flash',
      cause: 'exhausted',
      until: '2026-11-24T14:05:00.000Z',
      ...detail,
    },
    event_id: null,
  };
}
