// The hub's answer to a signed request (03-api.md section 1, `agent_core.signing.response_text`): seven lines
// `agent-response-v1`, the request's key ID, the request's nonce, the request's time, the HTTP status, the hub time
// `YYYY-MM-DDTHH:MM:SSZ` and `sha256_hex(body)`, signed with Ed25519 by the hub key published in `/v1/config`
// (`Agent-Hub-Key`, `Agent-Hub-Time`, `Agent-Hub-Signature`). 03-api.md lets browsers skip the check (SHOULD); this
// client makes it a MUST for every response it accepts as a success (WEB-02).
import { toHex, utf8 } from '../encoding/bytes';
import { parseSeconds } from '../encoding/time';
import { ed25519VerifyB64u } from '../crypto/keys';
import { sha256 } from '../crypto/subtle';

export const REQUEST_LABEL = 'agent-request-v1';
export const RESPONSE_LABEL = 'agent-response-v1';
/** The time window of the hub's request check, applied to the hub time of a response as well. */
export const WINDOW_SECONDS = 120;

export const HEADER_HUB_KEY = 'agent-hub-key';
export const HEADER_HUB_TIME = 'agent-hub-time';
export const HEADER_HUB_SIGNATURE = 'agent-hub-signature';

/** The hub's signing key from `GET /v1/config` (`hub_key`). */
export interface HubKey {
  readonly id: string;
  readonly publicKey: Uint8Array;
}

/** The signature parameters of the request a response answers. */
export interface SentRequest {
  readonly keyId: string;
  readonly nonce: string;
  readonly agentTime: string;
}

/** Lowercase hex SHA-256 of the body bytes (`sha256_hex`). */
export async function sha256Hex(body: Uint8Array): Promise<string> {
  return toHex(await sha256(body));
}

/** The eight lines of `agent-request-v1` (`agent_core.signing.request_text`). */
export function requestText(
  keyId: string,
  method: string,
  host: string,
  target: string,
  agentTime: string,
  nonce: string,
  bodyHash: string,
): Uint8Array<ArrayBuffer> {
  return utf8([REQUEST_LABEL, keyId, method, host, target, agentTime, nonce, bodyHash].join('\n'));
}

/** The seven lines of `agent-response-v1` (`agent_core.signing.response_text`). */
export function responseText(
  sent: SentRequest,
  status: number,
  hubTime: string,
  bodyHash: string,
): Uint8Array<ArrayBuffer> {
  return utf8([RESPONSE_LABEL, sent.keyId, sent.nonce, sent.agentTime, String(status), hubTime, bodyHash].join('\n'));
}

/** Why a response was refused: no signature headers, a wrong key or signature, or a hub time outside the window. */
export type ResponseRefusal = 'unsigned' | 'signature_invalid' | 'time_skew';

/**
 * Checks the response signature. Returns null for a valid signature whose hub time lies within `WINDOW_SECONDS` of
 * `nowMs`, `unsigned` if none of the three headers is present, otherwise the refusal.
 */
export async function checkResponse(
  hubKey: HubKey,
  sent: SentRequest,
  status: number,
  headers: Headers,
  body: Uint8Array,
  nowMs: number,
): Promise<ResponseRefusal | null> {
  const keyId = headers.get(HEADER_HUB_KEY);
  const hubTime = headers.get(HEADER_HUB_TIME);
  const signature = headers.get(HEADER_HUB_SIGNATURE);
  if (keyId === null && hubTime === null && signature === null) return 'unsigned';
  if (keyId !== hubKey.id || hubTime === null || signature === null) return 'signature_invalid';
  let seconds: number;
  try {
    seconds = parseSeconds(hubTime);
    await ed25519VerifyB64u(hubKey.publicKey, signature, responseText(sent, status, hubTime, await sha256Hex(body)));
  } catch {
    // every failure of these two calls is a CryptoError (malformed time, signature or key)
    return 'signature_invalid';
  }
  return Math.abs(seconds - Math.floor(nowMs / 1000)) > WINDOW_SECONDS ? 'time_skew' : null;
}
