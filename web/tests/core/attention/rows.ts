// Attention records for the tests of the situation view (KOR-39).
import type { AttentionRow } from '../../../src/core/store/slices/attention';

export const at = (minute: number) => `2026-11-21T12:${String(minute).padStart(2, '0')}:00.000Z`;

export function record(task: string, overrides: Partial<AttentionRow> = {}): AttentionRow {
  return {
    task_id: task,
    category: 'needs_decision',
    reason: 'question',
    ref_kind: 'operation',
    ref_id: '0190f000-0000-7000-8000-0000000000b1',
    since: at(0),
    seq: 1,
    subagents_total: 0,
    subagents_active: 0,
    updated_at: at(0),
    ...overrides,
  };
}
