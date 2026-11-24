// Shared helpers of the crypto unit tests: the error code of a refusal and fixed test keys from the vectors.
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { CryptoError } from '../../../src/core/crypto/errors';
import { Identity } from '../../../src/core/crypto/keys';
import { VECTOR_FILES } from './vector-suite';

/** The code of the `CryptoError` that `run` throws or rejects with; `'none'` if it succeeds. */
export async function codeOf(run: () => unknown): Promise<string> {
  try {
    await run();
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return 'none';
}

/** The fixed identity `alice`, `bob` or `device` of `contracts/vectors/keys.json`. */
export function actor(name: 'alice' | 'bob' | 'device'): Promise<Identity> {
  const data = VECTOR_FILES.keys.identities[name];
  return Identity.fromRaw(b64uDecode(data.kem_sk), b64uDecode(data.sig_sk));
}

/** The user ID of an actor. */
export const actorId = (name: 'alice' | 'bob' | 'device'): string => VECTOR_FILES.keys.identities[name].id;
