// The passkeys of the signed-in user (06-web.md section 3.6, 04-crypto.md section 5.6, 03-api.md section 4.1): the list
// of `GET /v1/me`, adding a passkey (signed `register/begin {add:true}`, `create()` with PRF, `register/finish`; the
// caller then signs in restricted to the new credential and uploads its slot), removing one (`DELETE
// /v1/passkeys/{id}`, refused for the last with `last_passkey`), and checking a recovery code against the recovery
// slot without rotating it (the "confirm" of the recovery-code reminder, section 3.2).
import { b64uDecode } from '../encoding/b64u';
import { parseTime } from '../encoding/time';
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import { creationOptions } from '../passkey/options';
import { attestationJson } from '../passkey/response';
import { isRecord } from '../registry';
import { createCredential, type Credentials } from './ceremony';
import { sameIdentity, unlockWithRecoveryCode } from './login';
import type { SignedRequest } from './recover';

/** One passkey of `GET /v1/me` → `passkeys`. */
export interface PasskeyRow {
  /** b64u credential ID. */
  readonly credentialId: string;
  /** Unix ms. */
  readonly createdAt: number;
  /** Unix ms, null if never used for a sign-in. */
  readonly lastUsedAt: number | null;
  /** The browser could derive `KEK_i` from this passkey's PRF output (its slot exists). */
  readonly prfSeen: boolean;
}

function time(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  try {
    return parseTime(value);
  } catch {
    return null;
  }
}

function row(value: unknown): PasskeyRow | null {
  if (!isRecord(value) || typeof value.credential_id !== 'string' || value.credential_id === '') return null;
  const createdAt = time(value.created_at);
  if (createdAt === null) return null;
  return {
    credentialId: value.credential_id,
    createdAt,
    lastUsedAt: time(value.last_used_at),
    prfSeen: value.prf_seen === true,
  };
}

/** The passkeys of a `GET /v1/me` body in the hub's order (newest first); malformed items are left out. */
export function passkeysOf(profile: Readonly<Record<string, unknown>> | null): PasskeyRow[] {
  const items = profile?.passkeys;
  if (!Array.isArray(items)) return [];
  return items.map(row).filter((item): item is PasskeyRow => item !== null);
}

/** Where `create()` should look for the new passkey. */
export type PasskeyPlace = 'this-device' | 'other-device';

/**
 * The creation options for `place`: "on another device" asks for a roaming authenticator, which makes the platform
 * offer its hybrid (QR code) and security-key UI (`authenticatorAttachment: "cross-platform"`, hint `hybrid`).
 */
export function placeOptions(
  options: PublicKeyCredentialCreationOptions,
  place: PasskeyPlace,
): PublicKeyCredentialCreationOptions {
  if (place === 'this-device') return options;
  return {
    ...options,
    authenticatorSelection: { ...options.authenticatorSelection, authenticatorAttachment: 'cross-platform' },
    hints: ['hybrid', 'security-key'],
  } as PublicKeyCredentialCreationOptions;
}

/**
 * Registers a further passkey for `userId` (signed `register/begin {add:true}`, gesture, `register/finish`); returns
 * its b64u credential ID. A cancelled gesture rejects with `CeremonyCancelled`.
 */
export async function registerPasskey(
  request: SignedRequest,
  credentials: Credentials,
  userId: string,
  place: PasskeyPlace,
): Promise<string> {
  const begun = await request('POST', '/v1/passkeys/register/begin', { body: { add: true } });
  if (!isRecord(begun) || begun.user_id !== userId || typeof begun.ceremony_id !== 'string') {
    fail('header_invalid');
  }
  const options = await creationOptions(begun.options);
  if (options.user.name !== userId) fail('header_invalid');
  const attestation = await createCredential(credentials, placeOptions(options, place));
  const credential = attestationJson(attestation);
  const finished = await request('POST', '/v1/passkeys/register/finish', {
    body: { ceremony_id: begun.ceremony_id, credential },
  });
  if (!isRecord(finished) || finished.user_id !== userId || finished.credential_id !== credential.id) {
    fail('header_invalid');
  }
  return credential.id;
}

/** The raw credential ID of a b64u ID (for `allowCredentials`). */
export function credentialBytes(credentialId: string): Uint8Array<ArrayBuffer> {
  return b64uDecode(credentialId);
}

/** `DELETE /v1/passkeys/{credential_id}`; the last passkey is refused by the hub (`last_passkey`). */
export async function removePasskey(request: SignedRequest, credentialId: string): Promise<void> {
  await request('DELETE', `/v1/passkeys/${credentialId}`);
}

/**
 * True if `codeText` opens the recovery slot `bundleCt` of `userId` to the identity in memory (no rotation).
 * Text that is no code fails with its code (`recovery_code_length`, `invite_code_invalid`), a wrong code with
 * `decrypt_failed`; a slot that opens to another identity is `decrypt_failed` as well.
 */
export async function checkRecoveryCode(
  userId: string,
  codeText: string,
  bundleCt: string,
  identity: Identity,
): Promise<void> {
  const opened = await unlockWithRecoveryCode(userId, codeText, bundleCt);
  const own = identity.public();
  if (!sameIdentity(opened, { kem_pub: own.kem, sig_pub: own.sig })) fail('decrypt_failed');
}
