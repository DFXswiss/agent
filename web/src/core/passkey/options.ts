// WebAuthn options from the hub's JSON (03-api.md section 4.1, contracts `api/objects.json#/$defs/creation_options`
// and `api/passkeys-login-begin.response.json`). The client checks every value it relies on for security instead of
// passing the hub's JSON through: ES256 only, resident key and user verification required (04-crypto.md section 9:
// UV on every ceremony), and the PRF input of 04-crypto.md section 3 (computed locally; a different value from the
// hub is refused). Malformed options are `header_invalid`.
import { b64uDecode } from '../encoding/b64u';
import { equalBytes, toHex } from '../encoding/bytes';
import { isPlainObject } from '../encoding/json';
import { fail } from '../crypto/errors';
import * as F from '../crypto/fields';
import { prfInput } from '../crypto/kdf';

/** The ceremony timeout of the hub (03-api.md section 4.1). */
export const TIMEOUT_MS = 300000;
export const ES256 = -7;
const TRANSPORTS = ['usb', 'nfc', 'ble', 'smart-card', 'hybrid', 'internal'];
const CHALLENGE_BYTES = 32;
const USER_HANDLE_BYTES = 16;

function object(value: unknown): Record<string, unknown> {
  if (!isPlainObject(value)) fail('header_invalid');
  return value;
}

/** The PRF extension (`extensions.prf.eval.first`), checked against the local PRF input. */
async function prfExtension(value: unknown): Promise<AuthenticationExtensionsClientInputs> {
  const extensions = F.requireKeys(value, ['prf']);
  const prf = F.requireKeys(extensions.prf, ['eval']);
  const evaluation = F.requireKeys(prf.eval, ['first']);
  const expected = await prfInput();
  if (!equalBytes(b64uDecode(evaluation.first), expected)) fail('header_invalid');
  return { prf: { eval: { first: expected } } };
}

/** A WebAuthn credential ID: 1 to 1023 bytes. */
function credentialId(raw: Uint8Array): Uint8Array<ArrayBuffer> {
  if (raw.length === 0 || raw.length > 1023) fail('header_invalid');
  return new Uint8Array(raw);
}

function credentialDescriptor(value: unknown): PublicKeyCredentialDescriptor {
  const item = object(value);
  const extra = Object.keys(item).filter((key) => !['type', 'id', 'transports'].includes(key));
  if (extra.length > 0 || item.type !== 'public-key') fail('header_invalid');
  const id = credentialId(b64uDecode(item.id));
  if (item.transports === undefined) return { type: 'public-key', id };
  const transports = item.transports;
  if (!Array.isArray(transports) || !transports.every((name) => TRANSPORTS.includes(name as string))) {
    fail('header_invalid');
  }
  return { type: 'public-key', id, transports: transports as AuthenticatorTransport[] };
}

function list(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) fail('header_invalid');
  return value;
}

/** `PublicKeyCredentialCreationOptions` from `options` of `POST /v1/passkeys/register/begin`. */
export async function creationOptions(json: unknown): Promise<PublicKeyCredentialCreationOptions> {
  const options = object(json);
  const known = ['rp', 'user', 'challenge', 'pubKeyCredParams', 'authenticatorSelection', 'extensions', 'timeout'];
  const optional = ['attestation', 'excludeCredentials'];
  const keys = Object.keys(options);
  if (
    !known.every((key) => keys.includes(key)) ||
    !keys.every((key) => known.includes(key) || optional.includes(key))
  ) {
    fail('header_invalid');
  }
  const rp = F.requireKeys(options.rp, ['id', 'name']);
  const user = F.requireKeys(options.user, ['id', 'name', 'displayName']);
  const userId = F.uuid(user.name);
  F.literal(user.displayName, userId);
  // the user handle is the 16 bytes of user_id (contracts: creation_options.user)
  const handle = b64uDecode(user.id, USER_HANDLE_BYTES);
  if (toHex(handle) !== userId.replace(/-/g, '')) fail('header_invalid');
  const params = list(options.pubKeyCredParams, 1);
  const param = F.requireKeys(params[0], ['type', 'alg']);
  F.literal(param.type, 'public-key');
  F.literal(param.alg, ES256);
  const selection = object(options.authenticatorSelection);
  const selectionKeys = Object.keys(selection);
  if (!selectionKeys.every((key) => ['residentKey', 'userVerification', 'requireResidentKey'].includes(key))) {
    fail('header_invalid');
  }
  F.literal(selection.residentKey, 'required');
  F.literal(selection.userVerification, 'required');
  if (selection.requireResidentKey !== undefined && selection.requireResidentKey !== true) fail('header_invalid');
  F.literal(options.timeout, TIMEOUT_MS);
  if (options.attestation !== undefined) F.literal(options.attestation, 'none');
  return {
    rp: { id: F.text(rp.id), name: F.text(rp.name) },
    user: { id: handle, name: userId, displayName: userId },
    challenge: b64uDecode(options.challenge, CHALLENGE_BYTES),
    pubKeyCredParams: [{ type: 'public-key', alg: ES256 }],
    authenticatorSelection: { residentKey: 'required', requireResidentKey: true, userVerification: 'required' },
    extensions: await prfExtension(options.extensions),
    timeout: TIMEOUT_MS,
    attestation: 'none',
    excludeCredentials: list(options.excludeCredentials ?? [], 64).map(credentialDescriptor),
  };
}

/**
 * `PublicKeyCredentialRequestOptions` from `options` of `POST /v1/passkeys/login/begin` with the challenge
 * `SHA-256(statement)` (04-crypto.md section 5.4). `allowCredential` restricts the ceremony to one credential (the
 * `get()` after adding a passkey, section 5.6); the hub itself always sends an empty list.
 */
export async function requestOptions(
  json: unknown,
  challenge: Uint8Array,
  allowCredential?: Uint8Array,
): Promise<PublicKeyCredentialRequestOptions> {
  const options = F.requireKeys(json, ['rpId', 'allowCredentials', 'userVerification', 'extensions', 'timeout']);
  list(options.allowCredentials, 0);
  F.literal(options.userVerification, 'required');
  F.literal(options.timeout, TIMEOUT_MS);
  F.requireBytes(challenge, CHALLENGE_BYTES);
  const allow: PublicKeyCredentialDescriptor[] = [];
  if (allowCredential !== undefined) allow.push({ type: 'public-key', id: credentialId(allowCredential) });
  return {
    rpId: F.text(options.rpId),
    challenge: new Uint8Array(challenge),
    allowCredentials: allow,
    userVerification: 'required',
    extensions: await prfExtension(options.extensions),
    timeout: TIMEOUT_MS,
  };
}
