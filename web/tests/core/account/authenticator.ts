// A fake WebAuthn authenticator for the account-flow tests (structurally a `Credentials`): `create()` makes a
// credential with a random ID and a per-credential PRF secret, `get()` asserts with the last (or chosen) credential.
// PRF output = SHA-256(secret || prf input) – stable per credential like a real authenticator. Knobs: no PRF, a
// cancelled ceremony (`NotAllowedError`/`AbortError`) or no credential at all, and `prfAtCreate = false` for an
// authenticator that answers PRF at `get()` without reporting it at `create()`.
import { concat, utf8 } from '../../../src/core/encoding/bytes';
import { random, sha256 } from '../../../src/core/crypto/subtle';
import type { Credentials } from '../../../src/core/account/ceremony';
import { newRecoveryCode } from '../../../src/core/account/recover';
import type { Identity } from '../../../src/core/crypto/keys';

export interface FakeCredential {
  readonly id: Uint8Array<ArrayBuffer>;
  readonly secret: Uint8Array<ArrayBuffer>;
}

export class FakeAuthenticator implements Credentials {
  readonly credentials: FakeCredential[] = [];
  prf = true;
  prfAtCreate = true;
  /** Next ceremony: throw a DOMException-like error with this name, or resolve null. */
  refuse: string | 'null' | null = null;
  use: FakeCredential | null = null;
  lastCreate: PublicKeyCredentialCreationOptions | null = null;
  lastGet: PublicKeyCredentialRequestOptions | null = null;

  #refusal(): Promise<null> | null {
    const refuse = this.refuse;
    if (refuse === null) return null;
    this.refuse = null;
    if (refuse === 'null') return Promise.resolve(null);
    const error = new Error('refused');
    error.name = refuse;
    return Promise.reject(error);
  }

  async #prf(credential: FakeCredential, input: BufferSource | undefined) {
    if (!this.prf || input === undefined) return {};
    const bytes = input instanceof ArrayBuffer ? new Uint8Array(input) : new Uint8Array((input as Uint8Array).buffer);
    return { prf: { enabled: true, results: { first: await sha256(concat(credential.secret, bytes)) } } };
  }

  async create(options: { publicKey: PublicKeyCredentialCreationOptions }): Promise<unknown> {
    const refused = this.#refusal();
    if (refused !== null) return refused;
    this.lastCreate = options.publicKey;
    const credential = { id: random(16), secret: random(32) };
    this.credentials.push(credential);
    const prf = this.prf && this.prfAtCreate ? { prf: { enabled: true } } : {};
    return {
      rawId: credential.id.buffer,
      response: {
        clientDataJSON: utf8('{"type":"webauthn.create"}'),
        attestationObject: random(64),
        getTransports: () => ['internal', 'carrier-pigeon'],
      },
      getClientExtensionResults: () => prf,
    };
  }

  async get(options: { publicKey: PublicKeyCredentialRequestOptions }): Promise<unknown> {
    const refused = this.#refusal();
    if (refused !== null) return refused;
    this.lastGet = options.publicKey;
    const credential = this.use ?? this.credentials[this.credentials.length - 1]!;
    const results = await this.#prf(credential, options.publicKey.extensions?.prf?.eval?.first);
    return {
      rawId: credential.id,
      response: {
        clientDataJSON: utf8('{"type":"webauthn.get"}'),
        authenticatorData: random(37),
        signature: random(70),
      },
      getClientExtensionResults: () => results,
    };
  }
}

/**
 * Gives a registered user a recovery code in the simulated hub (as the settings would: `newRecoveryCode` and the
 * replaced slot), for tests of accounts that have one – a registration makes none (KOR-22). Returns the code text.
 */
export async function giveRecoveryCode(
  hub: { users: Map<string, { bundles: Map<string, string>; recoveryId: string | null }> },
  registration: { readonly userId: string; readonly identity: Identity },
): Promise<string> {
  const prepared = await newRecoveryCode(registration.userId, registration.identity);
  const user = hub.users.get(registration.userId)!;
  user.bundles.set('recovery', prepared.body.bundle_ct);
  user.recoveryId = prepared.body.new_recovery_id;
  return prepared.code;
}
