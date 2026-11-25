// The user's connected agents (06-web.md section 8.3, 11-agent-access.md sections 4 and 5.3):
//
// - `loadAgents`: `GET /v1/me/agents` (newest first, as the hub sends them) into the `agents` slice; each label is
//   opened with the user's identity key (`crypto/agent.ts`), one that does not open is an unknown agent (label null).
//   Malformed rows are left out. `replaces` names the main orchestrator a row replaced at its confirmation (or null);
//   `remote` marks an agent paired from another computer (ADR-067; a row without it, from a hub of before KOR-42, is
//   not remote). `own_identity`, `held_identity`, `gen` and `revoked_reason` (ADR-082) are false/null in a row of a
//   hub that does not send them; `pub` and `cert` (when the hub sends them) only for the job. `fetchAgentRows` is the checked list without labels (the background job `agents`).
// - `revokeAgent` (`revoke.ts`, loaded when somebody disconnects): `DELETE /v1/me/agents/{id}`; `revokeAllAgents`: `DELETE /v1/me/agents` (→ the number revoked).
//   Both take effect at once at the hub; the list is loaded again afterwards, then the log clean-up runs at once
//   (`cleanUp`: the log job of every org of the user, which runs the job `agents` – the revoked agent is removed from
//   every log with its rotations – and the credential-key rotation after it, ADR-082 section 4).
// - `agentChanges`: how many notifications say that the user's agents changed elsewhere (`agent_paired_remote`, a
//   remote pairing confirmed in another client; `agent_revoked`); a new one makes the shell load the list again, so
//   the banner of a remote main orchestrator (ADR-067 section 4.4) appears in every open client, not only after the
//   next sign-in.
import { openLabel } from '../crypto/agent';
import { isUuid7 } from '../encoding/ids';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { RequestError, requestErrorCode } from '../requests/client';
import { ACCOUNT_KIND, type NotificationRow } from '../store/slices/notifications';
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

/** The `agent.account_change` actions after which the agent list is loaded again. */
export const LIST_CHANGES: readonly string[] = ['agent_paired_remote', 'agent_revoked'];

/** The number of notification rows that report a change of the user's agents (module comment). */
export function agentChanges(rows: Readonly<Record<string, NotificationRow>>): number {
  return Object.values(rows).filter(
    (row) => row.kind === ACCOUNT_KIND && LIST_CHANGES.includes(String(row.ref).split(':')[0]!),
  ).length;
}

const isTime = (value: unknown): value is string => typeof value === 'string' && !Number.isNaN(Date.parse(value));
const isTimeOrNull = (value: unknown): value is string | null => value === null || isTime(value);

export interface Row {
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
  readonly own_identity?: boolean;
  readonly held_identity?: boolean;
  readonly gen?: number | null;
  readonly revoked_reason?: string | null;
  /** The agent's public keys and certificate (hubs since KOR-57 stage b; checked by the job `agents`). */
  readonly pub?: { readonly kem: string; readonly sig: string } | null;
  readonly cert?: string | null;
}

const isPub = (value: unknown): boolean =>
  value === undefined ||
  value === null ||
  (isRecord(value) && typeof value.kem === 'string' && typeof value.sig === 'string');

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
    (value.remote === undefined || typeof value.remote === 'boolean') &&
    (value.own_identity === undefined || typeof value.own_identity === 'boolean') &&
    (value.held_identity === undefined || typeof value.held_identity === 'boolean') &&
    (value.gen === undefined ||
      value.gen === null ||
      (Number.isSafeInteger(value.gen) && (value.gen as number) >= 1)) &&
    (value.revoked_reason === undefined || value.revoked_reason === null || typeof value.revoked_reason === 'string') &&
    isPub(value.pub) &&
    (value.cert === undefined || value.cert === null || typeof value.cert === 'string')
  );
}

/** The checked rows of `GET /v1/me/agents` (malformed rows left out; a malformed answer is `unavailable`). */
export async function fetchAgentRows(client: Context['client']): Promise<Row[]> {
  const body = await client.request('GET', '/v1/me/agents');
  if (!isRecord(body) || !Array.isArray(body.agents)) throw new HubError('unavailable', 200);
  return body.agents.filter(isRow);
}

/** Loads and opens the list (module comment); failures land in the slice with their code. */
export async function loadAgents(context: Context): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const { userId, identity } = store.getState().me;
  setAgentsLoading(store);
  try {
    if (userId === null || identity === null) throw new RequestError('unauthorized');
    const entries: AgentEntry[] = [];
    for (const row of await fetchAgentRows(context.client)) {
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
        ownIdentity: row.own_identity === true,
        heldIdentity: row.held_identity === true,
        gen: row.gen ?? null,
        revokedReason: row.revoked_reason ?? null,
      });
    }
    if (store.generation === generation) setAgents(store, entries);
  } catch (error) {
    if (store.generation === generation) setAgentsError(store, requestErrorCode(error));
  }
}
