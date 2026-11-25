// Disconnecting agents from the settings or the banner (11-agent-access.md section 5.3, ADR-082 section 4): the hub
// refuses the key at once, the list is loaded again, then the log clean-up runs (the log job of every org, which runs
// the job `agents` – the revoked agent leaves every log with its rotations – and the credential-key rotation after
// it). Loaded only when somebody disconnects (initial JavaScript guard, 06-web.md section 10).
import { isRecord } from '../registry';
import type { TasksContext } from '../tasks/pull';
import { loadAgents } from './list';

type RevokeContext = Pick<TasksContext, 'store' | 'client' | 'refreshLog'>;

/** The log clean-up after a revocation (module comment): the log job of every org the store holds, one at a time. */
export async function cleanUp(context: RevokeContext): Promise<void> {
  for (const org of Object.keys(context.store.getState().orgs.byId).sort()) await context.refreshLog(org);
}

/** Disconnects one agent at once, reloads the list, then removes it from the logs. */
export async function revokeAgent(context: RevokeContext, agentId: string): Promise<void> {
  await context.client.request('DELETE', `/v1/me/agents/${encodeURIComponent(agentId)}`);
  await loadAgents(context);
  await cleanUp(context);
}

/** Disconnects every agent of the user at once, reloads the list, cleans up the logs; returns how many were revoked. */
export async function revokeAllAgents(context: RevokeContext): Promise<number> {
  const body = await context.client.request('DELETE', '/v1/me/agents');
  await loadAgents(context);
  await cleanUp(context);
  return isRecord(body) && Number.isSafeInteger(body.revoked) ? (body.revoked as number) : 0;
}
