// The work of the job "agents" (`jobs/agents.ts`, whose comment describes it), imported when a run needs it so that it
// stays out of the first load (06-web.md section 12.2).
import type { LogFields } from '../log/entries';
import type { OrgState } from '../log/state';
import type { VerifiedLog } from '../log/verify';
import { revokedAgents } from '../log/wraps';
import { verifyCert } from '../crypto/agent';
import { fetchAgentRows, type Row } from './list';
import { type OrgContext, type Planner, submitBatch } from '../org/batch';
import { isRecord } from '../registry';
import { orgEntry } from '../store/slices/orgs';
import { current, type JobContext, type JobRun } from '../jobs/index';
import { verifyOrgLog } from '../jobs/log-verify';
import { verifiedLogs } from '../jobs/credential-rewrap';

/** What the hub's list says about the user's agents. */
export interface HubAgents {
  /** Agents the hub has revoked or let expire. */
  readonly revoked: ReadonlySet<string>;
  /** The newest active own-identity agent, its generation and – with a certificate that verifies – its `agent_add`. */
  readonly active: { readonly id: string; readonly gen: number; readonly add?: LogFields | null } | null;
}

/**
 * The `agent_add` of a row whose pairing this browser confirmed with the row's certificate (`confirmed`: agent ID →
 * certificate) and whose keys and certificate the user's signing key `userSig` confirms, else null.
 */
async function certifiedAdd(
  row: Row,
  user: string,
  userSig: Uint8Array,
  confirmed: Readonly<Record<string, string>>,
): Promise<LogFields | null> {
  if (!isRecord(row.pub) || typeof row.cert !== 'string' || confirmed[row.agent_id] !== row.cert) return null;
  const fields = { agent: row.agent_id, pub: row.pub, user, gen: row.gen };
  try {
    await verifyCert(userSig, row.cert, fields);
  } catch {
    return null;
  }
  const pub = { kem: row.pub.kem, sig: row.pub.sig };
  return { type: 'agent_add', subject: row.agent_id, user, pub, gen: row.gen!, cert: row.cert };
}

/**
 * The hub's view (module comment) of the rows of `GET /v1/me/agents` of `user` (signing key `userSig`; `confirmed`:
 * the pairings this browser confirmed).
 */
export async function hubAgents(
  rows: readonly Row[],
  user: string,
  userSig: Uint8Array,
  confirmed: Readonly<Record<string, string>> = {},
): Promise<HubAgents> {
  const revoked = new Set(
    rows.filter((row) => row.state === 'revoked' || row.state === 'expired').map((r) => r.agent_id),
  );
  const found = rows.find((row) => row.state === 'active' && row.own_identity === true && typeof row.gen === 'number');
  if (found === undefined) return { revoked, active: null };
  return {
    revoked,
    active: { id: found.agent_id, gen: found.gen!, add: await certifiedAdd(found, user, userSig, confirmed) },
  };
}

/** The agent an entry adds (`agent_add`, or the `agent` member of `genesis`/`accept`) and its user, or null. */
function addedAgent(body: Readonly<Record<string, unknown>>): Record<string, unknown> | null {
  if (body.type === 'agent_add') return { ...body, id: body.subject };
  return isRecord(body.agent) ? { ...body.agent, user: body.subject } : null;
}

/** The `agent_add` fields of agent `id` with generation `gen` of `user` as a verified log names it, or null. */
export function agentAddOf(logs: readonly VerifiedLog[], user: string, id: string, gen: number): LogFields | null {
  for (const log of logs) {
    for (const { body } of log.entries) {
      const added = addedAgent(body);
      if (added?.id !== id || added.user !== user || added.gen !== gen) continue;
      const pub = added.pub as { kem: string; sig: string };
      return { type: 'agent_add', subject: id, user, pub, gen, cert: added.cert as string };
    }
  }
  return null;
}

/**
 * May agent `agent` of generation `gen` be added to this state: unknown here or removed only with its user (section
 * 9.2), and newer than the user's active agent.
 */
function addable(state: OrgState, user: string, agent: string, gen: number): boolean {
  const known = state.principals.get(agent);
  if (known !== undefined && !known.removedWithUser) return false;
  const active = state.activeAgent(user);
  return active === null || active.gen! < gen;
}

/** The batch of this org (module comment), or null; `logs` are the verified logs of all the user's orgs. */
export function agentsPlanner(hub: HubAgents, logs: readonly VerifiedLog[]): Planner {
  return (log, me) => {
    const state = log.state;
    const others = logs.filter((other) => other.state.org !== state.org);
    const revoked = new Set([...revokedAgents([...others.map((other) => other.state), state]), ...hub.revoked]);
    const active = state.activeAgent(me);
    if (active !== null && revoked.has(active.id)) return { entries: [{ type: 'remove', subject: active.id }] };
    const wanted = hub.active;
    if (wanted === null || revoked.has(wanted.id) || active?.id === wanted.id) return null;
    if (!addable(state, me, wanted.id, wanted.gen)) return null;
    const add = agentAddOf([...others, log], me, wanted.id, wanted.gen) ?? wanted.add ?? null;
    return add === null ? null : { entries: [add] };
  };
}

/** One run for `org`; returns whether a batch was sent. */
export async function syncAgents(context: JobContext, run: JobRun): Promise<boolean> {
  const { store } = context;
  if (store.getState().me.identity === null || orgEntry(store.getState().orgs, run.org).log === null) return false;
  const { userId, identity } = store.getState().me;
  const confirmed = context.prefs?.confirmedAgents(userId!) ?? {};
  const hub = await hubAgents(await fetchAgentRows(context.client), userId!, identity!.sigPublic(), confirmed);
  const orgContext: OrgContext = {
    store,
    client: context.client,
    now: context.now,
    refreshLog: (org) => verifyOrgLog(context, org, run.generation),
  };
  const planner = agentsPlanner(hub, verifiedLogs(store));
  const { built } = await submitBatch(orgContext, run.org, (log, me) =>
    current(context, run) ? planner(log, me) : null,
  );
  return built !== null;
}
