// The shared shape of events (04-crypto.md section 4.3) and credential records (section 4.4), twin of
// `agent_hub.crypto.signed`, adapted from Spike D `web/e2ee.js` (`sealEvent`, `verifyEventSignature`, `openEvent`).
//
// `{"header": {...}, "ct": b64u(AES-GCM(k, plaintext, AAD = canonical(header))), "sig": b64u(Ed25519_author(P ||
// label || canonical(header) || ct_raw))}` with `k = HKDF(scope_key, salt = header.salt, info = P || info_label)`
// and the nonce `header.nonce`. `seal` takes the header without `salt` and `nonce` and always draws both.
//
// Check order (identical in every implementation): object key set (`header_invalid`), header (key set, then
// `canonical` = `non_ascii`, then field formats), `ct` encoding (`header_invalid`), size (`size_exceeded`),
// signature (`signature_invalid`), decryption (`decrypt_failed`).
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat } from '../encoding/bytes';
import { canonical, isPlainObject } from '../encoding/json';
import * as aead from './aead';
import { fail } from './errors';
import { type Fields, requireKeys } from './fields';
import { SALT_BYTES } from './kdf';
import { ed25519Sign, ed25519VerifyB64u } from './keys';
import { label } from './labels';
import { Secret } from './secret';
import { random } from './subtle';

const OBJECT_KEYS = ['header', 'ct', 'sig'];
const RANDOM_FIELDS = ['salt', 'nonce'];
const PLACEHOLDER = {
  salt: b64uEncode(new Uint8Array(SALT_BYTES)),
  nonce: b64uEncode(new Uint8Array(aead.NONCE_BYTES)),
};

/** One signed AEAD format: header check, key derivation, signature label and size limit. */
export interface Format {
  readonly checkHeader: (value: unknown) => Fields;
  readonly deriveKey: (scopeKey: Secret, salt: Uint8Array) => Promise<CryptoKey>;
  readonly sigLabel: string;
  readonly maxCt: (header: Fields) => number;
}

/** A sealed event or credential record. */
export interface Sealed {
  header: Fields;
  ct: string;
  sig: string;
}

/** Checks a header without `salt` and `nonce` (these two are drawn by `seal`, never by the caller). */
export function checkFields(format: Format, fields: unknown): Fields {
  if (!isPlainObject(fields) || RANDOM_FIELDS.some((key) => Object.hasOwn(fields, key))) fail('header_invalid');
  format.checkHeader({ ...fields, ...PLACEHOLDER });
  return { ...fields };
}

/** `P || label || canonical(header) || ct_raw`. */
export function signatureText(format: Format, header: Fields, ctRaw: Uint8Array): Uint8Array<ArrayBuffer> {
  return concat(label(format.sigLabel), canonical(header), ctRaw);
}

/**
 * Encrypts and signs. `fields` is the header without `salt`/`nonce`: every call draws a fresh salt and nonce, so
 * re-sealing the same header never reuses key and nonce. `fixedSaltNonce` exists only for deterministic vectors.
 */
export async function seal(
  format: Format,
  scopeKey: Secret,
  fields: unknown,
  plaintext: Uint8Array,
  author: CryptoKey,
  fixedSaltNonce?: readonly [Uint8Array, Uint8Array],
): Promise<Sealed> {
  checkFields(format, fields);
  const [salt, nonce] = fixedSaltNonce ?? [random(SALT_BYTES), aead.randomNonce()];
  const header = format.checkHeader({ ...(fields as Fields), salt: b64uEncode(salt), nonce: b64uEncode(nonce) });
  const key = await format.deriveKey(scopeKey, salt);
  const ct = await aead.seal(key, nonce, plaintext, canonical(header));
  if (ct.length > format.maxCt(header)) fail('size_exceeded');
  const sig = await ed25519Sign(author, signatureText(format, header, ct));
  return { header, ct: b64uEncode(ct), sig: b64uEncode(sig) };
}

/** Checks structure, header, size and the author's signature without any key; returns header and raw ct. */
export async function verify(
  format: Format,
  sealed: unknown,
  authorSigPublic: Uint8Array,
): Promise<{ header: Fields; ct: Uint8Array<ArrayBuffer> }> {
  const data = requireKeys(sealed, OBJECT_KEYS);
  const header = format.checkHeader(data.header);
  const ct = b64uDecode(data.ct);
  if (ct.length > format.maxCt(header)) fail('size_exceeded');
  await ed25519VerifyB64u(authorSigPublic, data.sig, signatureText(format, header, ct));
  return { header, ct };
}

/** `verify`, then decrypt (`decrypt_failed` for a wrong scope key). */
export async function open(
  format: Format,
  scopeKey: Secret,
  sealed: unknown,
  authorSigPublic: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  const { header, ct } = await verify(format, sealed, authorSigPublic);
  const key = await format.deriveKey(scopeKey, b64uDecode(header.salt));
  return aead.open(key, b64uDecode(header.nonce), ct, canonical(header));
}
