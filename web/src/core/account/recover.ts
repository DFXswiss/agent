// Recovery with the recovery code (04-crypto.md section 5.5, 06-web.md section 3.5, 03-api.md section 4.1), for users
// without any working passkey: the code gives `recovery_id` and `KEK_rec` (the code itself never leaves the browser),
// `POST /v1/recovery/begin` answers the recovery slot and a challenge, the slot opens with `KEK_rec`, the identity signs
// `P || "recovery-proof\0" || user_id || "\0" || challenge`, and `POST /v1/recovery/finish` answers the creation
// options of a new passkey, registered with `create()` and `POST /v1/passkeys/register/finish`. The caller then signs
// in with the new passkey, uploads its slot and replaces the recovery code (`newRecoveryCode`, `replaceRecoveryCode`):
// mandatory after a recovery. A signed-in user may replace the code at any time in `/settings` (06-web.md section 3.2,
// KOR-01): `rotateRecoveryCode` proves the user with a fresh passkey assertion over the challenge of
// `POST /v1/me/recovery/begin`, so a session alone cannot plant a code.
//
// `429 rate_limited` of `recovery/begin` carries `Retry-After` (5 per hour and source, 20 per day and recovery ID); the
// anonymous client drops headers, so the requests of this module run through a `fetch` that remembers it, and the
// refusal becomes `RecoveryRateLimited` with the seconds to wait.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { isUuid7 } from '../encoding/ids';
import { openBundle, RECOVERY_SLOT, sealBundle } from '../crypto/bundle';
import { CryptoError, fail } from '../crypto/errors';
import { kekFromRecoveryCode } from '../crypto/kdf';
import type { Identity } from '../crypto/keys';
import {
  CHALLENGE_BYTES,
  formatRecoveryCode,
  generateRecoveryCode,
  parseRecoveryCode,
  recoveryId,
  signProof,
} from '../crypto/recovery';
import { HubError } from '../net/signedFetch';
import { creationOptions, requestOptions } from '../passkey/options';
import { prfEnabled } from '../passkey/prf';
import { attestationJson, certificateJson } from '../passkey/response';
import { isRecord } from '../registry';
import { anonClient, type AnonRequest } from './anon';
import { createCredential, type Credentials, getAssertion } from './ceremony';

/** `recovery/begin` was refused by a rate limit; `seconds` until the next attempt (null if the hub named none). */
export class RecoveryRateLimited extends Error {
  readonly seconds: number | null;

  constructor(seconds: number | null) {
    super('rate_limited');
    this.name = 'RecoveryRateLimited';
    this.seconds = seconds;
  }
}

/** The waiting time as text of `locale`: whole minutes below two hours, whole hours beyond, rounded up. */
export function waitText(seconds: number, locale: string): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  const [value, unit] = minutes < 120 ? [minutes, 'minute'] : [Math.ceil(minutes / 60), 'hour'];
  return new Intl.NumberFormat(locale, { style: 'unit', unit, unitDisplay: 'long' }).format(value);
}

/** What the recovery screen says about a refused begin: a message key (with parameters) or a catalog error code. */
export type RecoveryRefusal =
  { readonly key: string; readonly params: Readonly<Record<string, string>> } | { readonly code: string };

/** `429` → the waiting time (or "later"), `not_found` → one text for every unknown code, else the error code. */
export function recoveryRefusal(error: unknown, locale: string): RecoveryRefusal {
  if (error instanceof RecoveryRateLimited) {
    return error.seconds === null
      ? { key: 'recover.rate-limited.later', params: {} }
      : { key: 'recover.rate-limited', params: { time: waitText(error.seconds, locale) } };
  }
  const code = error instanceof CryptoError || error instanceof HubError ? error.code : 'unavailable';
  return code === 'not_found' ? { key: 'recover.unknown', params: {} } : { code };
}

/** Whole seconds of a `Retry-After` header (delta-seconds only, as the hub sends it), else null. */
export function retryAfterSeconds(value: string | null): number | null {
  if (value === null || !/^\d{1,9}$/.test(value.trim())) return null;
  return Number(value.trim());
}

/** The anonymous requests of the recovery, remembering `Retry-After` of the last `429`. */
export interface RecoveryClient {
  readonly anon: AnonRequest;
  /** `Retry-After` seconds of the last answer if it was a `429`, else null. */
  retryAfter(): number | null;
}

export function recoveryClient(fetch: typeof globalThis.fetch): RecoveryClient {
  let retryAfter: number | null = null;
  const watched: typeof globalThis.fetch = async (input, init) => {
    const response = await fetch(input, init);
    retryAfter = response.status === 429 ? retryAfterSeconds(response.headers.get('retry-after')) : null;
    return response;
  };
  return { anon: anonClient(watched), retryAfter: () => retryAfter };
}

/** The user found by the recovery code, with the identity opened from the recovery slot. */
export interface RecoveryStart {
  readonly userId: string;
  readonly identity: Identity;
  /** The 32-byte challenge of `recovery/begin` (TTL 300 s). */
  readonly challenge: Uint8Array;
}

/**
 * Step 1 and the decryption of step 2: `recovery_code_length`/`invite_code_invalid` for text that is no code,
 * `not_found` (HubError) for an unknown code, `RecoveryRateLimited` for `429`, `decrypt_failed` for a slot that does
 * not open with this code.
 */
export async function beginRecovery(client: RecoveryClient, codeText: string): Promise<RecoveryStart> {
  const code = parseRecoveryCode(codeText);
  const id = await recoveryId(code);
  const kek = await kekFromRecoveryCode(code);
  code.fill(0);
  let begun: unknown;
  try {
    begun = await client.anon('POST', '/v1/recovery/begin', { recovery_id: id });
  } catch (error) {
    if (error instanceof HubError && error.status === 429) throw new RecoveryRateLimited(client.retryAfter());
    throw error;
  }
  if (!isRecord(begun) || !isUuid7(begun.user_id) || typeof begun.bundle_ct !== 'string') fail('header_invalid');
  const challenge = b64uDecode(begun.challenge, CHALLENGE_BYTES);
  const identity = await openBundle(kek, begun.user_id, RECOVERY_SLOT, begun.bundle_ct);
  return { userId: begun.user_id, identity, challenge };
}

/** The new passkey of a recovery. */
export interface RecoveredPasskey {
  readonly userId: string;
  /** b64u credential ID. */
  readonly credentialId: string;
  /** PRF support reported by `create()` (informational; detection is per login). */
  readonly prfAtCreate: boolean;
}

/**
 * Step 2 (proof) and 3: `recovery/finish` with the proof, gesture 1 (`create()` with the options of the hub, checked by
 * `creationOptions` and bound to the recovered user), `register/finish` of the new passkey. A cancelled gesture rejects
 * with `CeremonyCancelled`; the proof ceremony is then spent and the caller begins again.
 */
export async function finishRecovery(
  client: RecoveryClient,
  credentials: Credentials,
  start: RecoveryStart,
): Promise<RecoveredPasskey> {
  const proof = await signProof(start.identity.sig.privateKey, start.userId, start.challenge);
  const finished = await client.anon('POST', '/v1/recovery/finish', {
    user_id: start.userId,
    challenge: b64uEncode(start.challenge),
    proof,
  });
  if (!isRecord(finished) || typeof finished.ceremony_id !== 'string' || finished.ceremony_id === '') {
    fail('header_invalid');
  }
  const options = await creationOptions(finished.options);
  if (options.user.name !== start.userId) fail('header_invalid');
  const attestation = await createCredential(credentials, options);
  const credential = attestationJson(attestation);
  const registered = await client.anon('POST', '/v1/passkeys/register/finish', {
    ceremony_id: finished.ceremony_id,
    credential,
  });
  if (!isRecord(registered) || registered.user_id !== start.userId || registered.credential_id !== credential.id) {
    fail('header_invalid');
  }
  return {
    userId: start.userId,
    credentialId: credential.id,
    prfAtCreate: prfEnabled(attestation.getClientExtensionResults()),
  };
}

/** A new recovery code with its slot, ready for `PUT /v1/me/bundles/recovery`. */
export interface NewRecoveryCode {
  /** `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`, shown once. */
  readonly code: string;
  readonly body: { readonly bundle_ct: string; readonly new_recovery_id: string };
}

/** Draws a new recovery code and seals the identity for the recovery slot with its `KEK_rec`. */
export async function newRecoveryCode(userId: string, identity: Identity): Promise<NewRecoveryCode> {
  const raw = generateRecoveryCode();
  const code = formatRecoveryCode(raw);
  const bundle = await sealBundle(await kekFromRecoveryCode(raw), userId, RECOVERY_SLOT, identity);
  const id = await recoveryId(raw);
  raw.fill(0);
  return { code, body: { bundle_ct: bundle, new_recovery_id: id } };
}

/** A signed request (`SignedClient.request`). */
export type SignedRequest = (method: string, target: string, options?: { body: unknown }) => Promise<unknown>;

/**
 * Replaces the recovery slot and ID (`PUT /v1/me/bundles/recovery`). The hub allows it once after a recovery; when a
 * repeat is refused with `forbidden` because the first attempt was stored but its answer lost, the stored slot equals
 * ours and the replacement counts as done.
 */
export async function replaceRecoveryCode(request: SignedRequest, prepared: NewRecoveryCode): Promise<void> {
  try {
    await request('PUT', '/v1/me/bundles/recovery', { body: prepared.body });
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'forbidden') throw error;
    const stored = await request('GET', '/v1/me/bundles/recovery');
    if (!isRecord(stored) || stored.bundle_ct !== prepared.body.bundle_ct) throw error;
  }
}

/**
 * Replaces the recovery code of the signed-in user at any time (06-web.md section 3.2; KOR-01): `POST
 * /v1/me/recovery/begin` (a ceremony of this user), one `get()` over its challenge (any passkey of the user; a
 * cancelled gesture rejects with `CeremonyCancelled` and nothing changes), then `PUT /v1/me/bundles/recovery` with the
 * prepared slot and the assertion. Call it only after the user confirmed the new code; the old code stays valid until
 * the hub accepted the new one.
 */
export async function rotateRecoveryCode(
  request: SignedRequest,
  credentials: Credentials,
  prepared: NewRecoveryCode,
): Promise<void> {
  const begun = await request('POST', '/v1/me/recovery/begin', { body: {} });
  if (!isRecord(begun) || typeof begun.ceremony_id !== 'string') fail('header_invalid');
  const options = await requestOptions(begun.options, b64uDecode(begun.challenge, CHALLENGE_BYTES));
  const signed = certificateJson(new Uint8Array(0), await getAssertion(credentials, options));
  const assertion = {
    ceremony_id: begun.ceremony_id,
    credential_id: signed.credential_id,
    authenticator_data: signed.authenticator_data,
    client_data_json: signed.client_data_json,
    signature: signed.signature,
  };
  await request('PUT', '/v1/me/bundles/recovery', { body: { ...prepared.body, assertion } });
}
