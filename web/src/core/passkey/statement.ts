// The session statement `agent-session-v1` (04-crypto.md sections 4.9 and 5.4), reused unchanged from
// `agent_core.passkey.certificate` (PASSKEY-PROTOCOL.md section 6.2): UTF-8 text of five lines joined by `\n`
// without a trailing line feed – label, origin, b64u of the 65-byte session public key, b64u of the 32-byte hub
// value, expiry `YYYY-MM-DDTHH:MM:SSZ`. The WebAuthn challenge is SHA-256 over these bytes; the session key ID is
// `session.` and the first 32 hex characters of the same digest.
import { b64uEncode } from '../encoding/b64u';
import { copy, toHex, utf8 } from '../encoding/bytes';
import { isPrintableAscii } from '../encoding/json';
import { formatSeconds } from '../encoding/time';
import { fail } from '../crypto/errors';
import { P256_POINT_BYTES } from '../crypto/keys';
import { attempt, sha256, subtle } from '../crypto/subtle';

export const STATEMENT_LABEL = 'agent-session-v1';
/** The hub issues certificates for 43080 s and accepts at most 12 h (03-api.md section 12). */
export const SESSION_ISSUE_SECONDS = 43080;
export const SESSION_MAX_SECONDS = 43200;
export const HUB_VALUE_BYTES = 32;
const SESSION_KEY_PREFIX = 'session.';

export interface StatementInput {
  /** `location.origin` of the page. */
  readonly origin: string;
  /** The 65-byte uncompressed P-256 point of the session key. */
  readonly sessionPublicKey: Uint8Array;
  /** `hub_value` of `login/begin` (32 bytes). */
  readonly hubValue: Uint8Array;
  /** Expiry in whole Unix seconds. */
  readonly expires: number;
}

/** The statement bytes; a malformed input (also a point that is not on P-256) is `header_invalid`. */
export async function buildStatement(input: StatementInput): Promise<Uint8Array<ArrayBuffer>> {
  const { origin, sessionPublicKey, hubValue, expires } = input;
  if (origin === '' || !isPrintableAscii(origin)) fail('header_invalid');
  if (sessionPublicKey.length !== P256_POINT_BYTES || sessionPublicKey[0] !== 0x04) fail('header_invalid');
  if (hubValue.length !== HUB_VALUE_BYTES) fail('header_invalid');
  const point = await attempt(() =>
    subtle.importKey('raw', copy(sessionPublicKey), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']),
  );
  if (point === null) fail('header_invalid');
  const lines = [STATEMENT_LABEL, origin, b64uEncode(sessionPublicKey), b64uEncode(hubValue), formatSeconds(expires)];
  return utf8(lines.join('\n'));
}

/** The WebAuthn challenge of a sign-in: SHA-256 over the statement bytes. */
export function statementChallenge(statement: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return sha256(statement);
}

/** `session.` and the first 16 bytes of SHA-256 over the statement bytes as lowercase hex. */
export async function sessionKeyId(statement: Uint8Array): Promise<string> {
  return SESSION_KEY_PREFIX + toHex((await sha256(statement)).subarray(0, 16));
}

/** The expiry the browser writes: its clock plus `SESSION_ISSUE_SECONDS` (whole seconds). */
export function sessionExpiry(nowMs: number): number {
  return Math.floor(nowMs / 1000) + SESSION_ISSUE_SECONDS;
}
