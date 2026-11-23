// Identity bundle (04-crypto.md section 4.1), twin of `agent_hub.crypto.bundle`, after Spike D `web/e2ee.js`.
// Plaintext `canonical({"kem","sig","v":1})`, ciphertext `b64u(nonce12 || AES-GCM(KEK, plaintext, AAD))` with
// `AAD = P || "bundle\0" || user_id || "\0" || slot`. Slots: `passkey:<b64u(credential_id)>` and `recovery`.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, equalBytes, utf8 } from '../encoding/bytes';
import { canonical, isPrintableAscii, parseJson } from '../encoding/json';
import * as aead from './aead';
import { fail } from './errors';
import { requireInt, requireKeys } from './fields';
import { Identity, RAW_KEY_BYTES } from './keys';
import { label, LABEL_BUNDLE } from './labels';

export const RECOVERY_SLOT = 'recovery';
const PASSKEY_SLOT_RE = /^passkey:[A-Za-z0-9_-]+$/;
const BUNDLE_KEYS = ['kem', 'sig', 'v'];

/** The slot name of a passkey: `passkey:<b64u(credential_id)>`. */
export function passkeySlot(credentialId: Uint8Array): string {
  if (credentialId.length === 0) fail('header_invalid');
  return `passkey:${b64uEncode(credentialId)}`;
}

/** `recovery` or a passkey slot with a canonical, non-empty credential ID (`header_invalid` otherwise). */
export function checkSlot(slot: unknown): string {
  if (slot === RECOVERY_SLOT) return slot;
  if (typeof slot !== 'string' || !PASSKEY_SLOT_RE.test(slot)) fail('header_invalid');
  b64uDecode(slot.slice('passkey:'.length));
  return slot;
}

/** `P || "bundle\0" || user_id || "\0" || slot`; both parts printable ASCII (`non_ascii`). */
export function bundleAad(userId: unknown, slot: unknown): Uint8Array<ArrayBuffer> {
  if (typeof userId !== 'string' || userId === '') fail('header_invalid');
  if (!isPrintableAscii(userId)) fail('non_ascii');
  return concat(label(LABEL_BUNDLE), utf8(`${userId}\0${checkSlot(slot)}`));
}

/** `canonical({"kem": b64u(x25519 private), "sig": b64u(ed25519 seed), "v": 1})`. */
export function bundlePlaintext(identity: Identity): Uint8Array<ArrayBuffer> {
  return identity.revealPrivate((kem, sig) => canonical({ kem: b64uEncode(kem), sig: b64uEncode(sig), v: 1 }));
}

/** The identity of a bundle plaintext; exact key set, `v == 1`, canonical bytes (`header_invalid`). */
export async function parseBundlePlaintext(plaintext: Uint8Array): Promise<Identity> {
  const data = requireKeys(parseJson(plaintext), BUNDLE_KEYS);
  requireInt(data.v, 1, 1);
  if (!equalBytes(canonical(data), plaintext)) fail('header_invalid');
  return Identity.fromRaw(b64uDecode(data.kem, RAW_KEY_BYTES), b64uDecode(data.sig, RAW_KEY_BYTES));
}

/** Encrypts the identity for one slot; `nonce` only for test vectors. */
export async function sealBundle(
  kek: CryptoKey,
  userId: string,
  slot: string,
  identity: Identity,
  nonce?: Uint8Array,
): Promise<string> {
  const aad = bundleAad(userId, slot);
  return b64uEncode(await aead.sealBlob(kek, bundlePlaintext(identity), aad, nonce));
}

/** Decrypts a slot (`decrypt_failed` for a wrong KEK, user or slot) and parses the identity. */
export async function openBundle(kek: CryptoKey, userId: string, slot: string, sealed: string): Promise<Identity> {
  const aad = bundleAad(userId, slot);
  return parseBundlePlaintext(await aead.openBlob(kek, b64uDecode(sealed), aad));
}
