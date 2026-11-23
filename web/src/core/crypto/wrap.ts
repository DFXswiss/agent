// Key wrap (04-crypto.md section 4.2), twin of `agent_hub.crypto.wrap`, after Spike D `web/e2ee.js`.
// `wrap = b64u(enc || ct)`: HPKE single-shot seal of the 32-byte scope key to the recipient's X25519 key with
// `info = P || "wrap\0" || canonical({"epoch","recipient","scope","scope_id"})` and empty AAD. The wrapper signs
// `P || "wrap\0" || canonical({"epoch","recipient","scope","scope_id","wrap"})` with its Ed25519 key.
// Who may wrap for whom (W1-W3) is decided by src/core/log, not here.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat } from '../encoding/bytes';
import { canonical } from '../encoding/json';
import { fail } from './errors';
import { requireInt } from './fields';
import * as hpke from './hpke';
import { KEY_BYTES } from './kdf';
import { ed25519Sign, ed25519VerifyB64u, type KeyPair } from './keys';
import { label, LABEL_WRAP } from './labels';
import { Secret } from './secret';

export const SCOPES = ['org', 'wg', 'cred', 'community'] as const;
export type Scope = (typeof SCOPES)[number];

/** What a wrap is bound to. */
export interface WrapTarget {
  scope: string;
  scope_id: string;
  epoch: number;
  recipient: string;
}

function fields(target: WrapTarget): Record<string, unknown> {
  const { scope, scope_id: scopeId, epoch, recipient } = target;
  if (!(SCOPES as readonly unknown[]).includes(scope) || typeof scopeId !== 'string' || typeof recipient !== 'string') {
    fail('header_invalid');
  }
  if (scopeId === '' || recipient === '') fail('header_invalid');
  return { epoch: requireInt(epoch, 1), recipient, scope, scope_id: scopeId };
}

/** The HPKE `info` that binds a wrap to scope, scope ID, epoch and recipient. */
export function wrapInfo(target: WrapTarget): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_WRAP), canonical(fields(target)));
}

/** Wraps a 32-byte scope key for one recipient; `ephemeral` only for test vectors. */
export async function wrapKey(
  recipientKemPublic: Uint8Array,
  target: WrapTarget,
  key: Secret,
  ephemeral?: Uint8Array,
): Promise<string> {
  if (key.length !== KEY_BYTES) fail('header_invalid');
  const info = wrapInfo(target);
  return b64uEncode(await key.reveal((raw) => hpke.seal(recipientKemPublic, info, raw, ephemeral)));
}

/** Opens a wrap (`hpke_short`, `decrypt_failed`); anything but a 32-byte key is `decrypt_failed`. */
export async function unwrapKey(recipient: KeyPair, target: WrapTarget, wrap: string): Promise<Secret> {
  const info = wrapInfo(target);
  const key = await hpke.open(recipient, info, b64uDecode(wrap));
  if (key.length !== KEY_BYTES) fail('decrypt_failed');
  return Secret.of(key);
}

/** `P || "wrap\0" || canonical({epoch, recipient, scope, scope_id, wrap})`. */
export function wrapSignatureText(target: WrapTarget, wrap: string): Uint8Array<ArrayBuffer> {
  const values = fields(target);
  b64uDecode(wrap);
  return concat(label(LABEL_WRAP), canonical({ ...values, wrap }));
}

/** The wrapper's b64u Ed25519 signature over `wrapSignatureText`. */
export async function signWrap(wrapper: CryptoKey, target: WrapTarget, wrap: string): Promise<string> {
  return b64uEncode(await ed25519Sign(wrapper, wrapSignatureText(target, wrap)));
}

/** Checks the wrapper's signature (`signature_invalid`); possible without any key. */
export async function verifyWrap(
  wrapperSigPublic: Uint8Array,
  target: WrapTarget,
  wrap: string,
  signature: unknown,
): Promise<void> {
  await ed25519VerifyB64u(wrapperSigPublic, signature, wrapSignatureText(target, wrap));
}
