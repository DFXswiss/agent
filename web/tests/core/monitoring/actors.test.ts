// KOR-52 (ADR-095 section 4): the agent actors of the audit – the page's `actors` as sent (keyed by session IDs), each
// entry checked against `audit_agent` when a row names it, and the actor's kind, handle, main agent and person.
import { describe, expect, it } from 'vitest';
import { agentActor, isAuditAgent } from '../../../src/core/monitoring/actors';
import { parseAuditPage } from '../../../src/core/monitoring/audit';
import { auditRow, DEV, ORG, S1, S2, T0, TASK, WG } from './fixtures';

describe('agent actors (ADR-095 section 4)', () => {
  const agent = (extra: Record<string, unknown> = {}) => ({
    task_id: TASK,
    number: 42,
    agent_path: null,
    parent_session_id: null,
    ...extra,
  });

  it('keeps the well-formed agents of a page keyed by session', () => {
    const actors = {
      [S1]: agent(),
      [S2]: agent({ agent_path: '1.2', parent_session_id: S1 }),
      'not-a-session': agent(),
      [WG]: agent({ number: 0 }),
    };
    // as sent, keyed by session IDs only (the entries are checked where a row names them)
    expect(parseAuditPage({ rows: [], next_before: null, actors }).actors).toEqual({
      [S1]: agent(),
      [S2]: agent({ agent_path: '1.2', parent_session_id: S1 }),
      [WG]: agent({ number: 0 }),
    });
    expect(parseAuditPage({ rows: [], next_before: null, actors: 'x' }).actors).toEqual({});
    expect(isAuditAgent(agent())).toBe(true);
    for (const bad of [
      null,
      agent({ task_id: 'x' }),
      agent({ task_id: 7 }),
      agent({ number: 1.5 }),
      agent({ agent_path: '0' }),
      agent({ agent_path: 3 }),
      agent({ parent_session_id: 3 }),
    ]) {
      expect(isAuditAgent(bad)).toBe(false);
    }
  });

  it('names the main agent or the subagent, its main agent and the person it acted for', () => {
    const actors = { [S1]: agent(), [S2]: agent({ agent_path: '1.2', parent_session_id: S1 }) };
    const row = (extra: Record<string, unknown>) => parseAuditPage({ rows: [auditRow(1, T0, extra)] }).rows[0]!;
    expect(agentActor(row({ actor_kind: 'main_agent', actor_id: S1, on_behalf_of: ORG }), actors)).toEqual({
      kind: 'main_agent',
      handle: '#42',
      main: '#42',
      task: TASK,
      onBehalfOf: ORG,
    });
    expect(agentActor(row({ actor_kind: 'subagent', actor_id: S2 }), actors)).toEqual({
      kind: 'subagent',
      handle: '#42.1.2',
      main: '#42',
      task: TASK,
      onBehalfOf: null,
    });
    // a subagent the hub gives no path is named by its main agent
    expect(agentActor(row({ actor_kind: 'subagent', actor_id: S1 }), actors)!.handle).toBe('#42');
    expect(agentActor(row({ actor_kind: 'main_agent', actor_id: DEV }), actors)).toBeNull();
    // an entry that breaks `audit_agent` names no agent
    expect(agentActor(row({ actor_kind: 'main_agent', actor_id: WG }), { [WG]: agent({ number: 0 }) })).toBeNull();
    expect(agentActor(row({ actor_kind: 'main_agent', actor_id: 'toString' }), actors)).toBeNull();
    expect(agentActor(row({ actor_kind: 'main_agent', actor_id: null }), actors)).toBeNull();
    expect(agentActor(row({ actor_kind: 'user' }), actors)).toBeNull();
  });
});
