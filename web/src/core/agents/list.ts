// The user's connected agents (06-web.md section 8.3, 11-agent-access.md sections 4 and 5.3):
//
// - `loadAgents`: `GET /v1/me/agents` (newest first, as the hub sends them) into the `agents` slice; each label is
//   opened with the user's identity key (`crypto/agent.ts`), one that does not open is an unknown agent (label null).
//   Malformed rows are left out. `replaces` names the main orchestrator a row replaced at its confirmation (or null);
//   `remote` marks an agent paired from another computer (ADR-062; a row without it, from a hub of before KOR-42, is
//   not remote).
// - `revokeAgent`: `DELETE /v1/me/agents/{id}`; `revokeAllAgents`: `DELETE /v1/me/agents` (→ the number revoked).
//   Both take effect at once at the hub; the list is loaded again afterwards.
import { openLabel } from '../crypto/agent';
import { isUuid7 } from '../encoding/ids';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { RequestError, requestErrorCode } from '../requests/client';
import {
  type AgentEntry,
  AGENT_STATES,
  type AgentState,
  setAgents,
  setAgentsError,
  setAgentsLoading,
} from '../store/slices/agents';
import type { TasksContext } from '../tasks/pull';

type Context = Pick<TasksContext, 'store' | 'client'>;

const isTime = (value: unknown): value is string => typeof value === 'string' && !Number.isNaN(Date.parse(value));
const isTimeOrNull = (value: unknown): value is string | null => value === null || isTime(value);

interface Row {
  readonly agent_id: string;
  readonly state: AgentState;
  readonly label_ct: string;
  readonly created_at: string;
  readonly confirmed_at: string;
  readonly claimed_at: string | null;
  readonly last_used_at: string | null;
  readonly revoked_at: string | null;
  readonly replaces: string | null;
  readonly remote?: boolean;
}

function isRow(value: unknown): value is Row {
  return (
    isRecord(value) &&
    isUuid7(value.agent_id) &&
    (AGENT_STATES as readonly unknown[]).includes(value.state) &&
    typeof value.label_ct === 'string' &&
    isTime(value.created_at) &&
    isTime(value.confirmed_at) &&
    isTimeOrNull(value.claimed_at) &&
    isTimeOrNull(value.last_used_at) &&
    isTimeOrNull(value.revoked_at) &&
    (value.replaces === null || isUuid7(value.replaces)) &&
    (value.remote === undefined || typeof value.remote === 'boolean')
  );
}

/** Loads and opens the list (module comment); failures land in the slice with their code. */
export async function loadAgents(context: Context): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const { userId, identity } = store.getState().me;
  setAgentsLoading(store);
  try {
    if (userId === null || identity === null) throw new RequestError('unauthorized');
    const body = await context.client.request('GET', '/v1/me/agents');
    if (!isRecord(body) || !Array.isArray(body.agents)) throw new HubError('unavailable', 200);
    const entries: AgentEntry[] = [];
    for (const row of body.agents.filter(isRow)) {
      const label = await openLabel(identity.kem, row.agent_id, userId, row.label_ct).catch(() => null);
      entries.push({
        agentId: row.agent_id,
        state: row.state,
        label,
        createdAt: row.created_at,
        confirmedAt: row.confirmed_at,
        claimedAt: row.claimed_at,
        lastUsedAt: row.last_used_at,
        revokedAt: row.revoked_at,
        replaces: row.replaces,
        remote: row.remote === true,
      });
    }
    if (store.generation === generation) setAgents(store, entries);
  } catch (error) {
    if (store.generation === generation) setAgentsError(store, requestErrorCode(error));
  }
}

/** Disconnects one agent at once, then reloads the list. */
export async function revokeAgent(context: Context, agentId: string): Promise<void> {
  await context.client.request('DELETE', `/v1/me/agents/${encodeURIComponent(agentId)}`);
  await loadAgents(context);
}

/** Disconnects every agent of the user at once, then reloads the list; returns how many were revoked. */
export async function revokeAllAgents(context: Context): Promise<number> {
  const body = await context.client.request('DELETE', '/v1/me/agents');
  await loadAgents(context);
  return isRecord(body) && Number.isSafeInteger(body.revoked) ? (body.revoked as number) : 0;
}
