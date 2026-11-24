// The two WebAuthn calls of the sign-in flows behind a structural interface, so the flows run unchanged against
// `navigator.credentials` in the browser and against test doubles in Node. A ceremony the user (or the platform)
// cancels – `AbortError`, `NotAllowedError` (06-web.md section 3.1) or no credential at all – becomes
// `CeremonyCancelled`; the screens return to the previous step with "try again".
import type { AssertionResult, AttestationResult } from '../passkey/response';
import type { PrfExtensionResults } from '../passkey/prf';

/** What `create()` and `get()` return, as far as the flows read it. */
export interface CredentialResult {
  readonly rawId: ArrayBuffer | ArrayBufferView;
  getClientExtensionResults(): PrfExtensionResults;
}

export type Attestation = CredentialResult & AttestationResult;
export type Assertion = CredentialResult & AssertionResult;

/** `navigator.credentials` as far as the flows use it. */
export interface Credentials {
  create(options: { publicKey: PublicKeyCredentialCreationOptions }): Promise<unknown>;
  get(options: { publicKey: PublicKeyCredentialRequestOptions }): Promise<unknown>;
}

/** The user cancelled a passkey ceremony (or it timed out); nothing was registered or signed in. */
export class CeremonyCancelled extends Error {
  constructor() {
    super('ceremony_cancelled');
    this.name = 'CeremonyCancelled';
  }
}

const CANCEL_NAMES = new Set(['AbortError', 'NotAllowedError']);

/** True for the DOMException names a cancelled or refused ceremony raises. */
export function isCancel(error: unknown): boolean {
  return error instanceof Error && CANCEL_NAMES.has(error.name);
}

async function run<T>(call: () => Promise<unknown>): Promise<T> {
  let result: unknown;
  try {
    result = await call();
  } catch (error) {
    if (isCancel(error)) throw new CeremonyCancelled();
    throw error;
  }
  if (result === null || result === undefined) throw new CeremonyCancelled();
  return result as T;
}

/** `create()` (gesture 1 of a registration). */
export function createCredential(credentials: Credentials, publicKey: PublicKeyCredentialCreationOptions) {
  return run<Attestation>(() => credentials.create({ publicKey }));
}

/** `get()` (the sign-in gesture). */
export function getAssertion(credentials: Credentials, publicKey: PublicKeyCredentialRequestOptions) {
  return run<Assertion>(() => credentials.get({ publicKey }));
}
