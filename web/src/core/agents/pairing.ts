// Confirming the pairing of the user's AI agent in the browser (11-agent-access.md section 5.1, ADR-056 section 3):
//
// 1. `lookupPairing`: `GET /v1/agent-pairings/{id}` (anonymous); the keys the hub returns must have the fingerprint of
//    the link (`pairing_mismatch`, a client-only code: the hub swapped the keys or the link is forged) and the pairing
//    must be `open` (else `pairing_unknown`: expired, used or cancelled).
// 2. `preparePairing`: draws the handover secret `r` (32 bytes) and computes the body of `POST /v1/me/agents` – the
//    identity sealed to the agent under `k`, the label sealed to the user's own identity key, the claim hash. The body
//    is computed once per screen: a retry after `pairing_gesture_required` (a new passkey gesture) sends the same
//    values, so a confirmation that reached the hub but whose answer was lost is answered alike, not `id_conflict`.
// 3. `submitPairing`: `POST /v1/me/agents`, signed by the session of the pairing-tagged login ceremony.
// 4. `loopbackForm`: the top-level form POST the same tab submits to the agent's loopback listener; only there does
//    `r` leave the browser. `r` is a form field, never part of a URL (it would stay in the browser history).
import { claim, claimHash, fingerprint, sealIdentity, sealLabel } from '../crypto/agent';
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import { Secret } from '../crypto/secret';
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { parseTime } from '../encoding/time';
import type { AnonRequest } from '../account/anon';
import { HubError, type SignedClient } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { PairingLink } from './link';

/** How long a pairing stays open (11-agent-access.md section 2: created + 600 s). */
export const PAIRING_TTL_MS = 600_000;

export interface Pairing {
  readonly pairingId: string;
  readonly kemPub: Uint8Array;
  readonly sigPub: Uint8Array;
  /** Unix ms of the deadline, and of the request (deadline − 10 min). */
  readonly expiresAt: number;
  readonly requestedAt: number;
}

/** The pairing of `link` at the hub, checked against the link's fingerprint (module comment). */
export async function lookupPairing(anon: AnonRequest, link: PairingLink): Promise<Pairing> {
  const body = await anon('GET', `/v1/agent-pairings/${encodeURIComponent(link.pairingId)}`);
  if (!isRecord(body) || !isRecord(body.pub) || body.pairing_id !== link.pairingId) fail('header_invalid');
  const kemPub = b64uDecode(body.pub.kem, 32);
  const sigPub = b64uDecode(body.pub.sig, 32);
  const expiresAt = parseTime(body.expires_at);
  if ((await fingerprint(kemPub, sigPub)) !== link.fingerprint) throw new HubError('pairing_mismatch', 0);
  if (body.state !== 'open') throw new HubError('pairing_unknown', 404);
  return { pairingId: link.pairingId, kemPub, sigPub, expiresAt, requestedAt: expiresAt - PAIRING_TTL_MS };
}

export interface PreparedPairing {
  readonly pairingId: string;
  /** The confirming user (the agent needs it to derive the claim and to open the identity). */
  readonly userId: string;
  readonly secret: Secret;
  readonly body: {
    readonly pairing_id: string;
    readonly bundle_ct: string;
    readonly label_ct: string;
    readonly claim_hash: string;
  };
}

/** The handover secret and the confirmation body for `userId` (module comment). */
export async function preparePairing(
  pairing: Pairing,
  link: PairingLink,
  userId: string,
  identity: Identity,
): Promise<PreparedPairing> {
  const secret = Secret.random(32);
  const id = pairing.pairingId;
  const label = { host: link.host, name: link.name };
  return {
    pairingId: id,
    userId,
    secret,
    body: {
      pairing_id: id,
      bundle_ct: await sealIdentity(pairing.kemPub, secret, id, userId, identity),
      label_ct: await sealLabel(identity.kemPublic(), id, userId, label),
      claim_hash: b64uEncode(await claimHash(await claim(secret, id, userId))),
    },
  };
}

/** `POST /v1/me/agents`; the hub answers with the agent ID, which is the pairing ID. */
export async function submitPairing(client: Pick<SignedClient, 'request'>, prepared: PreparedPairing): Promise<void> {
  const answer = await client.request('POST', '/v1/me/agents', { body: prepared.body });
  if (!isRecord(answer) || answer.agent_id !== prepared.pairingId) fail('header_invalid');
}

export interface LoopbackForm {
  /** `http://127.0.0.1:<port>/agent-pairing`. */
  readonly action: string;
  /** The form fields in order: pairing ID `p`, user ID `u`, `r` (b64u) and the platform root key `k` of the build. */
  readonly fields: readonly (readonly [string, string])[];
}

/** The handover to the agent (module comment). */
export function loopbackForm(port: number, prepared: PreparedPairing, rootKey: string): LoopbackForm {
  return {
    action: `http://127.0.0.1:${port}/agent-pairing`,
    fields: [
      ['p', prepared.pairingId],
      ['u', prepared.userId],
      ['r', prepared.secret.reveal((raw) => b64uEncode(raw))],
      ['k', rootKey],
    ],
  };
}
