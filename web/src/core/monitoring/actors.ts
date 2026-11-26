// The agent actors of the audit (ADR-095 section 4, 06-web.md section 4.0a): a `main_agent` / `subagent` actor is a
// session, and the page's `actors` map (`parseAuditPage`, as sent) names its task, its number and its place. Checked
// here, when a row names it: an entry that breaks `audit_agent` names no agent, and the view shows the actor by its
// kind. Only the audit view loads this module (it stays out of the first load).
import { agentPath, mainHandle, subagentHandle } from '../agents/name';
import { isRecord } from '../registry';
import { type AuditActors, type AuditRow, isUuid } from './audit';

/** `audit_agent` of the contracts: the task, number and place of an agent actor. */
export interface AuditAgent {
  readonly task_id: string;
  readonly number: number;
  readonly agent_path: string | null;
  readonly parent_session_id: string | null;
}

export function isAuditAgent(value: unknown): value is AuditAgent {
  return (
    isRecord(value) &&
    typeof value.task_id === 'string' &&
    isUuid(value.task_id) &&
    Number.isSafeInteger(value.number) &&
    (value.number as number) >= 1 &&
    (value.agent_path === null || agentPath(value) !== null) &&
    (value.parent_session_id === null || typeof value.parent_session_id === 'string')
  );
}

/** The agent actor of a row: its kind, its handle and its main agent's handle, its task and the person it acted for. */
export interface AgentActor {
  readonly kind: 'main_agent' | 'subagent';
  readonly handle: string;
  /** The handle of the main agent (equal to `handle` for a main agent). */
  readonly main: string;
  readonly task: string;
  readonly onBehalfOf: string | null;
}

/** The agent actor of `row` when the page's `actors` name it (null for other actors and unknown agents). */
export function agentActor(row: AuditRow, actors: AuditActors): AgentActor | null {
  if ((row.actor_kind !== 'main_agent' && row.actor_kind !== 'subagent') || row.actor_id === null) return null;
  const agent = Object.hasOwn(actors, row.actor_id) ? actors[row.actor_id] : null;
  if (!isAuditAgent(agent)) return null;
  const main = mainHandle(agent.number);
  const sub = row.actor_kind === 'subagent' && agent.agent_path !== null;
  return {
    kind: row.actor_kind,
    handle: sub ? subagentHandle(agent.number, agent.agent_path!) : main,
    main,
    task: agent.task_id,
    onBehalfOf: row.on_behalf_of ?? null,
  };
}
