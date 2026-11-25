// The lookup of a pairing by the page that shows it (`/connect`, and `/invite` when the link carries a pairing): kept
// apart from the confirmation (`pairing.ts`) so the invitation screen loads none of the confirmation's batches and key
// rotation (initial JavaScript guard, 06-web.md section 10).
import type { AnonRequest } from '../account/anon';
import { agentCode, fingerprint, NONCE_BYTES } from '../crypto/agent';
import { fail } from '../crypto/errors';
import { b64uDecode } from '../encoding/b64u';
import { parseTime } from '../encoding/time';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { PairingLink } from './link';

/** How long a pairing stays open (11-agent-access.md section 2: created + 600 s). */
export const PAIRING_TTL_MS = 600_000;
/** How long a remote pairing stays open (ADR-067 section 4.3: created + 300 s). */
export const REMOTE_PAIRING_TTL_MS = 300_000;

export interface Pairing {
  readonly pairingId: string;
  readonly kemPub: Uint8Array;
  readonly sigPub: Uint8Array;
  /** Unix ms of the deadline, and of the request (deadline − 10 min, remote: − 5 min). */
  readonly expiresAt: number;
  readonly requestedAt: number;
  /** A pairing from another computer (link and hub agree). */
  readonly remote: boolean;
  /** The agent code the user must type (remote only; never shown by the page), else null. */
  readonly agentCode: string | null;
}

/**
 * The pairing of `link` at the hub, checked against the link's fingerprint and mode (module comment); `origin` is
 * the page's origin (`scheme://host[:port]`), an input of the agent code.
 */
export async function lookupPairing(anon: AnonRequest, link: PairingLink, origin: string): Promise<Pairing> {
  const body = await anon('GET', `/v1/agent-pairings/${encodeURIComponent(link.pairingId)}`);
  if (!isRecord(body) || !isRecord(body.pub) || body.pairing_id !== link.pairingId) fail('header_invalid');
  if (body.remote !== undefined && body.remote !== true) fail('header_invalid');
  const kemPub = b64uDecode(body.pub.kem, 32);
  const sigPub = b64uDecode(body.pub.sig, 32);
  const expiresAt = parseTime(body.expires_at);
  const remote = body.remote === true;
  if ((await fingerprint(kemPub, sigPub)) !== link.fingerprint) throw new HubError('pairing_mismatch', 0);
  if (remote !== (link.mode === 'remote')) throw new HubError('pairing_mismatch', 0);
  if (body.state !== 'open') throw new HubError('pairing_unknown', 404);
  const code = remote
    ? await agentCode(link.pairingId, link.fingerprint, b64uDecode(body.nonce, NONCE_BYTES), origin)
    : null;
  const ttl = remote ? REMOTE_PAIRING_TTL_MS : PAIRING_TTL_MS;
  return {
    pairingId: link.pairingId,
    kemPub,
    sigPub,
    expiresAt,
    requestedAt: expiresAt - ttl,
    remote,
    agentCode: code,
  };
}
