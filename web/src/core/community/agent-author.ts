// The author of a community message that is an own-identity agent (ADR-082): the hub's `agent` entry names its user,
// the user's signing key, the agent's KEM key, generation and certificate; the certificate must verify under that key.
// Loaded by the community engine only when such an author appears (initial JavaScript guard, 06-web.md section 10).
import { verifyCert } from '../crypto/agent';
import { b64uDecode } from '../encoding/b64u';
import { isRecord } from '../registry';

/** The agent's user (`id`) and its signing key (`sigPub`, to be pinned by the caller), or null when the entry is malformed
 * or its certificate does not verify. */
export async function agentUser(
  agentId: string,
  sigPub: string,
  agent: unknown,
): Promise<{ readonly id: string; readonly sigPub: string } | null> {
  const text = (value: unknown): value is string => typeof value === 'string';
  if (!isRecord(agent) || !text(agent.user) || !text(agent.user_sig_pub) || !text(agent.kem_pub)) return null;
  try {
    await verifyCert(b64uDecode(agent.user_sig_pub, 32), agent.cert, {
      agent: agentId,
      pub: { kem: agent.kem_pub, sig: sigPub },
      user: agent.user,
      gen: agent.gen,
    });
  } catch {
    return null;
  }
  return { id: agent.user, sigPub: agent.user_sig_pub };
}
