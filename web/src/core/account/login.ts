// Sign-in and unlock (04-crypto.md sections 4.9 and 5.4, 06-web.md section 3.4): `login/begin`, a new
// non-extractable P-256 session key, the `agent-session-v1` statement, one `get()` with challenge
// SHA-256(statement) and the PRF extension, `login/finish`. The same gesture certifies the session key and yields
// the PRF output that opens the passkey slot of the identity bundle. Without PRF output (or without a passkey slot)
// the result says so and the caller offers the recovery code (unlock only, no rotation).
import { b64uDecode } from '../encoding/b64u';
import { isUuid7 } from '../encoding/ids';
import { parseTime } from '../encoding/time';
import { openBundle, passkeySlot, RECOVERY_SLOT, sealBundle } from '../crypto/bundle';
import { fail } from '../crypto/errors';
import { kekFromPrf, kekFromRecoveryCode } from '../crypto/kdf';
import { generateSessionKey, type Identity } from '../crypto/keys';
import { parseRecoveryCode } from '../crypto/recovery';
import type { Secret } from '../crypto/secret';
import { requestOptions } from '../passkey/options';
import { prfOutput } from '../passkey/prf';
import { certificateJson } from '../passkey/response';
import { buildStatement, sessionExpiry, sessionKeyId, statementChallenge } from '../passkey/statement';
import { isRecord } from '../registry';
import type { AnonRequest } from './anon';
import { type Credentials, getAssertion } from './ceremony';

/** A certified session and what the gesture returned for the unlock. */
export interface SignIn {
  readonly userId: string;
  /** `session.<32 hex>`. */
  readonly keyId: string;
  /** `expires_at` (Unix ms). */
  readonly expiresAt: number;
  /** The non-extractable P-256 private key of the session. */
  readonly privateKey: CryptoKey;
  /** Its 65-byte uncompressed point. */
  readonly publicKey: Uint8Array;
  /** b64u credential ID of the passkey used. */
  readonly credentialId: string;
  /** The PRF output of this login, null without PRF (`prf_missing`). */
  readonly prf: Secret | null;
  /** The passkey slot of the bundle, null if none was uploaded for this passkey. */
  readonly bundleCt: string | null;
  /** The hub expects PRF for this passkey (its slot exists). */
  readonly prfExpected: boolean;
}

export interface SignInInput {
  readonly anon: AnonRequest;
  readonly credentials: Credentials;
  /** `location.origin`. */
  readonly origin: string;
  readonly now?: () => number;
  /** Restricts `get()` to this credential (the unlock of a passkey just added, 04-crypto.md section 5.6). */
  readonly allowCredential?: Uint8Array;
}

function text(value: unknown): string {
  if (typeof value !== 'string' || value === '') fail('header_invalid');
  return value;
}

/** Runs the login ceremony; a cancelled gesture rejects with `CeremonyCancelled`. */
export async function signIn(input: SignInInput): Promise<SignIn> {
  const { anon, credentials, origin, now = Date.now, allowCredential } = input;
  const begun = await anon('POST', '/v1/passkeys/login/begin', {});
  if (!isRecord(begun)) fail('header_invalid');
  const ceremonyId = text(begun.ceremony_id);
  const hubValue = b64uDecode(begun.hub_value);
  const sessionKey = await generateSessionKey();
  const statement = await buildStatement({
    origin,
    sessionPublicKey: sessionKey.publicKey,
    hubValue,
    expires: sessionExpiry(now()),
  });
  const options = await requestOptions(begun.options, await statementChallenge(statement), allowCredential);
  const assertion = await getAssertion(credentials, options);
  const certificate = certificateJson(statement, assertion);
  const prf = prfOutput(assertion.getClientExtensionResults());
  const finished = await anon('POST', '/v1/passkeys/login/finish', { ceremony_id: ceremonyId, certificate });
  if (!isRecord(finished)) fail('header_invalid');
  const userId = text(finished.user_id);
  if (!isUuid7(userId) || finished.key_id !== (await sessionKeyId(statement))) fail('header_invalid');
  const bundle = finished.bundle_ct;
  return {
    userId,
    keyId: finished.key_id,
    expiresAt: parseTime(finished.expires_at),
    privateKey: sessionKey.privateKey,
    publicKey: sessionKey.publicKey,
    credentialId: certificate.credential_id,
    prf,
    bundleCt: typeof bundle === 'string' ? bundle : null,
    prfExpected: finished.prf_expected === true,
  };
}

/** The passkey slot name of a sign-in. */
export function slotOf(signIn: Pick<SignIn, 'credentialId'>): string {
  return passkeySlot(b64uDecode(signIn.credentialId));
}

/**
 * Opens the passkey slot with `KEK_i` from the PRF output. Null when this sign-in cannot unlock (no PRF output or no
 * slot): the `prf_missing` case. A slot that does not open is `decrypt_failed`.
 */
export async function unlockWithPrf(signIn: SignIn): Promise<Identity | null> {
  if (signIn.prf === null || signIn.bundleCt === null) return null;
  const kek = await kekFromPrf(signIn.prf, b64uDecode(signIn.credentialId));
  return openBundle(kek, signIn.userId, slotOf(signIn), signIn.bundleCt);
}

/** Opens the recovery slot with the recovery code text (`recovery_code_length`, `invite_code_invalid`, `decrypt_failed`). */
export async function unlockWithRecoveryCode(userId: string, codeText: string, bundleCt: string): Promise<Identity> {
  const code = parseRecoveryCode(codeText);
  const kek = await kekFromRecoveryCode(code);
  code.fill(0);
  return openBundle(kek, userId, RECOVERY_SLOT, bundleCt);
}

/** The passkey slot of `identity` for the PRF output of this sign-in, or null without PRF output. */
export async function passkeyBundle(signIn: SignIn, identity: Identity): Promise<string | null> {
  if (signIn.prf === null) return null;
  const kek = await kekFromPrf(signIn.prf, b64uDecode(signIn.credentialId));
  return sealBundle(kek, signIn.userId, slotOf(signIn), identity);
}

/** True if `identity` has the public keys the hub registered for the user (`GET /v1/me` → `identity`). */
export function sameIdentity(identity: Identity, registered: unknown): boolean {
  if (!isRecord(registered) || typeof registered.kem_pub !== 'string' || typeof registered.sig_pub !== 'string') {
    return false;
  }
  const own = identity.public();
  return own.kem === registered.kem_pub && own.sig === registered.sig_pub;
}
