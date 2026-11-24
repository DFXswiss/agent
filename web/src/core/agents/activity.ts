// "Security activity" of the connected-agents settings (11-agent-access.md section 7): the user's account audit
// (`GET /v1/me/audit`) – sign-in, passkeys, recovery, agents, and every row written by a request of one of the user's
// agents – newest first in pages; rows as in the org audit (`monitoring/audit.ts`), the agent marker by `agent_id`.
import { accountAuditTarget, type AuditPage, parseAuditPage } from '../monitoring/audit';
import type { SignedClient } from '../net/signedFetch';

/** One page of the account audit (`before`: the cursor of the previous page). */
export async function loadAccountAudit(
  client: Pick<SignedClient, 'request'>,
  before: string | null = null,
): Promise<AuditPage> {
  return parseAuditPage(await client.request('GET', accountAuditTarget(before)));
}
