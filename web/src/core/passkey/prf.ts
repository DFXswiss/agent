// PRF results of a WebAuthn ceremony (04-crypto.md sections 3 and 5.1/5.4, ADR-006). The output `prf_i` must be
// exactly 32 bytes (`prf_length`); an absent output (exported passkey, password manager without PRF) is `null` for
// the caller to show the `prf_missing` guidance, or `prf_missing` from `requirePrf`. The output is returned as a
// `Secret` (06-web.md section 2.3) and the caller's buffer is zeroed.
import { fail } from '../crypto/errors';
import { KEY_BYTES } from '../crypto/kdf';
import { Secret } from '../crypto/secret';

type Bytes = ArrayBuffer | ArrayBufferView;

/** The PRF part of `getClientExtensionResults()` (structural, so test doubles fit). */
export interface PrfExtensionResults {
  readonly prf?: { readonly enabled?: boolean; readonly results?: { readonly first?: Bytes } };
}

function view(value: Bytes): Uint8Array {
  return value instanceof ArrayBuffer
    ? new Uint8Array(value)
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
}

/** The PRF output `prf_i` or `null` if the authenticator returned none; a wrong length is `prf_length`. */
export function prfOutput(results: PrfExtensionResults): Secret | null {
  const first = results.prf?.results?.first;
  if (first === undefined) return null;
  const raw = view(first);
  if (raw.length !== KEY_BYTES) fail('prf_length');
  const secret = Secret.of(raw);
  raw.fill(0);
  return secret;
}

/** `prfOutput`, refusing an absent output with `prf_missing`. */
export function requirePrf(results: PrfExtensionResults): Secret {
  return prfOutput(results) ?? fail('prf_missing');
}

/** True if the authenticator reported PRF support at `create()` (`prf.enabled`) or already returned an output. */
export function prfEnabled(results: PrfExtensionResults): boolean {
  return results.prf?.enabled === true || results.prf?.results?.first !== undefined;
}
