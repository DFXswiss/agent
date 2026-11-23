// Registration through an invitation (04-crypto.md section 5.1 steps 1, 3 and 4; 03-api.md section 4.1): the lookup
// of the code's `invite_id`, then `register/begin` (ES256 only, checked by `creationOptions`), gesture 1
// (`create()` with PRF) and `register/finish` with the identity created in this browser, the recovery slot sealed
// with `KEK_rec`, the self-wrap of the user's credential key `CK_1`, signed by the new identity, and the invitation
// proof `inv_sig` (ADR-043: `sk_inv` of the code signs the ceremony challenge, so that only the code's holder can
// register through an invitation whose `invite_id` others can read). The code, the recovery code, PRF outputs and
// private keys never leave the browser: the hub receives only `invite_id`, `recovery_id`, public keys, signatures and
// ciphertext.
import { isUuid7 } from '../encoding/ids';
import { RECOVERY_SLOT, sealBundle } from '../crypto/bundle';
import { fail } from '../crypto/errors';
import { inviteId, inviteSecrets, parseCode, signRegister } from '../crypto/invite';
import { kekFromRecoveryCode } from '../crypto/kdf';
import { Identity } from '../crypto/keys';
import { formatRecoveryCode, generateRecoveryCode, recoveryId } from '../crypto/recovery';
import { Secret } from '../crypto/secret';
import { signWrap, wrapKey } from '../crypto/wrap';
import { creationOptions } from '../passkey/options';
import { prfEnabled } from '../passkey/prf';
import { attestationJson } from '../passkey/response';
import { isRecord } from '../registry';
import type { AnonRequest } from './anon';
import { createCredential, type Credentials } from './ceremony';

/** The answer of `POST /v1/invites/lookup` (fields the flows read). */
export interface Lookup {
  readonly inviteId: string;
  readonly kind: string;
  readonly scope: 'platform' | 'org';
  readonly expiresAt: string;
  readonly registered: boolean;
  /** The whole answer, for the org path (package, log). */
  readonly raw: Readonly<Record<string, unknown>>;
}

/** Parses the code text of the fragment (`invite_code_length`, `invite_code_invalid`) and derives `invite_id`. */
export async function codeToInviteId(text: string): Promise<string> {
  return inviteId(parseCode(text));
}

/** `POST /v1/invites/lookup`; unknown, used, revoked and expired invitations are `invite_unknown` (HubError). */
export async function lookupInvite(anon: AnonRequest, id: string): Promise<Lookup> {
  const body = await anon('POST', '/v1/invites/lookup', { invite_id: id });
  if (!isRecord(body) || (body.scope !== 'platform' && body.scope !== 'org')) fail('header_invalid');
  return {
    inviteId: id,
    kind: String(body.kind),
    scope: body.scope,
    expiresAt: String(body.expires_at),
    registered: body.registered === true,
    raw: body,
  };
}

/** A user registered by this browser: the identity and the recovery code exist only here, in memory. */
export interface Registration {
  readonly userId: string;
  /** b64u credential ID of the new passkey. */
  readonly credentialId: string;
  readonly identity: Identity;
  /** The recovery code as shown to the user (`XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`). */
  readonly recoveryCode: string;
  /** Whether the authenticator reported PRF support at `create()` (informational; detection is per login). */
  readonly prfAtCreate: boolean;
}

function text(value: unknown): string {
  if (typeof value !== 'string' || value === '') fail('header_invalid');
  return value;
}

/**
 * Registers a new user through the invitation of the code text `inviteCode`: begin, gesture 1, keys, finish. A cancelled
 * gesture rejects with `CeremonyCancelled` (the hub forgets the abandoned ceremony; the next attempt starts with a new
 * begin).
 */
export async function register(anon: AnonRequest, credentials: Credentials, inviteCode: string): Promise<Registration> {
  const secrets = await inviteSecrets(parseCode(inviteCode));
  const begun = await anon('POST', '/v1/passkeys/register/begin', { invite_id: secrets.inviteId });
  if (!isRecord(begun)) fail('header_invalid');
  const userId = text(begun.user_id);
  if (!isUuid7(userId)) fail('header_invalid');
  const ceremonyId = text(begun.ceremony_id);
  const options = await creationOptions(begun.options);
  if (options.user.name !== userId) fail('header_invalid');
  const attestation = await createCredential(credentials, options);
  const credential = attestationJson(attestation);
  // the identity, the recovery slot and CK_1 (04-crypto.md section 5.1 step 4)
  const identity = await Identity.generate();
  const code = generateRecoveryCode();
  const recoveryCode = formatRecoveryCode(code);
  const bundle = await sealBundle(await kekFromRecoveryCode(code), userId, RECOVERY_SLOT, identity);
  const recovery = { recovery_id: await recoveryId(code), bundle_ct: bundle };
  code.fill(0);
  const credentialKey = Secret.random(32);
  const target = { scope: 'cred', scope_id: userId, epoch: 1, recipient: userId };
  const wrap = await wrapKey(identity.kemPublic(), target, credentialKey);
  const sig = await signWrap(identity.sig.privateKey, target, wrap);
  const identityPub = identity.public();
  const invSig = await signRegister(secrets, userId, options.challenge as Uint8Array);
  const finished = await anon('POST', '/v1/passkeys/register/finish', {
    ceremony_id: ceremonyId,
    credential,
    identity: { kem_pub: identityPub.kem, sig_pub: identityPub.sig },
    recovery,
    cred_wrap: { epoch: 1, wrap, sig },
    inv_sig: invSig,
  });
  if (!isRecord(finished) || finished.user_id !== userId || finished.credential_id !== credential.id) {
    fail('header_invalid');
  }
  return {
    userId,
    credentialId: credential.id,
    identity,
    recoveryCode,
    prfAtCreate: prfEnabled(attestation.getClientExtensionResults()),
  };
}
