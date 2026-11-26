// KOR-52 (ADR-095 section 1): the session tree takes a subagent's place from the hub (`agent_path`, unique within the
// task also across main sessions) and falls back to the creation order of an older hub; nodes have handles.
import { describe, expect, it } from 'vitest';
import { flatten, nodeFacts, nodeHandle, ownIdentity, sessionMeta, sessionTree } from '../../../src/core/sessions/tree';
import type { SessionEntry, SessionRow, TasksState } from '../../../src/core/store/slices/tasks';

const TASK = 't1';

function entry(id: string, minute: number, extra: Record<string, unknown> = {}): SessionEntry {
  const row = {
    session_id: id,
    task_id: TASK,
    org_id: 'o',
    wg_id: 'w',
    device_id: null,
    state: 'working',
    state_reason: null,
    estimate_ms: null,
    created_at: `2026-11-21T12:0${minute}:00.000Z`,
    first_working_at: null,
    ...extra,
  } as unknown as SessionRow;
  return { row, transitions: [], estimatedAt: null };
}

const sessions = (...entries: SessionEntry[]): TasksState['sessions'] =>
  Object.fromEntries(entries.map((item) => [item.row.session_id, item]));

describe('agent paths', () => {
  it('uses the hub path when it is well-formed, else the creation order under the parent', () => {
    const tree = sessionTree(
      sessions(
        entry('m1', 0),
        entry('c1', 1, { parent_session_id: 'm1', depth: 1, agent_path: '1' }),
        entry('c11', 2, { parent_session_id: 'c1', depth: 2, agent_path: '1.1' }),
        entry('m2', 3),
        // a child of the later main session continues the task's numbering
        entry('c2', 4, { parent_session_id: 'm2', depth: 1, agent_path: '2' }),
        // an older hub: no path, the position under the parent
        entry('c3', 5, { parent_session_id: 'm2', depth: 1, agent_path: null }),
        entry('c4', 6, { parent_session_id: 'm2', depth: 1, agent_path: '1.' }),
      ),
      TASK,
    );
    const nodes = flatten(tree).map((node) => [node.entry.row.session_id, node.path.join('.')]);
    expect(nodes).toEqual([
      ['m1', ''],
      ['c1', '1'],
      ['c11', '1.1'],
      ['m2', ''],
      ['c2', '2'],
      ['c3', '2'],
      ['c4', '3'],
    ]);
    expect(sessionMeta(entry('x', 0, { agent_path: '3.12' })).agentPath).toEqual([3, 12]);
    expect(sessionMeta(entry('x', 0)).agentPath).toBeNull();
  });

  it('gives handles with the task number', () => {
    expect(nodeHandle({ depth: 0, path: [] }, 42)).toBe('#42');
    expect(nodeHandle({ depth: 2, path: [1, 2] }, 42)).toBe('#42.1.2');
    expect(nodeHandle({ depth: 0, path: [] }, null)).toBeNull();
  });
});

describe('identities', () => {
  it('names the identity of a session that acts with another one than its task', () => {
    expect(ownIdentity({ entry: entry('c', 0, { github_identity: 'i2' }) }, 'i1')).toBe('i2');
    expect(ownIdentity({ entry: entry('c', 0, { github_identity: 'i1' }) }, 'i1')).toBeNull();
    expect(ownIdentity({ entry: entry('c', 0, { github_identity: null }) }, 'i1')).toBeNull();
    expect(ownIdentity({ entry: entry('c', 0) }, null)).toBeNull();
    expect(ownIdentity({ entry: entry('c', 0, { github_identity: 'i2' }) }, null)).toBe('i2');
  });
});

describe('node facts', () => {
  it('list the own identity and, for a subagent, the requesting operation', () => {
    const child = entry('c', 0, { github_identity: 'i2', origin_operation_id: 'op' });
    expect(nodeFacts({ entry: child, depth: 1 }, 'i1')).toEqual({ identities: ['i2'], origins: ['op'] });
    expect(nodeFacts({ entry: child, depth: 0 }, 'i2')).toEqual({ identities: [], origins: [] });
    expect(nodeFacts({ entry: entry('c', 0), depth: 1 }, null)).toEqual({ identities: [], origins: [] });
  });
});
