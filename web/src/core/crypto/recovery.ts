// Recovery codes (04-crypto.md sections 3, 4.1 and 5.5), twin of `agent_hub.crypto.recovery`.
// `recovery_id = b64u(HKDF(code16, info=P||"recovery-id")[:16])`, `KEK_rec` in `kdf.ts`, and the proof
// `Ed25519_identity(P || "recovery-proof\0" || user_id || "\0" || challenge)` over the raw 32-byte challenge of
// `POST /v1/recovery/begin`. The code text has the form of invitation codes; a wrong length is `recovery_code_length`.
//
// An **unset** recovery slot (KOR-22, ADR-055, 04-crypto.md section 4.1): a registration whose passkey reported PRF
// shows no code. Its slot is sealed with a random code that is dropped at once – nobody can ever use it – over
// zero bytes of the bundle's length, under the nonce `HKDF(ikm=ed25519 seed, info=P||"recovery-unset\0"||user_id)
// [:12]`. Only the holder of the identity can tell this nonce from a random one, so the browser knows that no code
// exists while the hub sees an ordinary slot.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, equalBytes, utf8 } from '../encoding/bytes';
import { isPrintableAscii } from '../encoding/json';
import { NONCE_BYTES, sealBlob } from './aead';
import { bundleAad, bundlePlaintext, RECOVERY_SLOT } from './bundle';
import { fail } from './errors';
import { requireBytes } from './fields';
import { formatCodeText, parseCodeText } from './invite';
import { CODE_BYTES, hkdf, kekFromRecoveryCode, recoveryIdRaw } from './kdf';
import { ed25519Sign, ed25519VerifyB64u, type Identity } from './keys';
import { label, LABEL_RECOVERY_PROOF, RECOVERY_UNSET } from './labels';
import { random } from './subtle';

export const CHALLENGE_BYTES = 32;

/** 16 random bytes. */
export function generateRecoveryCode(): Uint8Array<ArrayBuffer> {
  return random(CODE_BYTES);
}

/** The grouped base32 text (`recovery_code_length` unless 16 bytes). */
export function formatRecoveryCode(code: Uint8Array): string {
  return formatCodeText(code, 'recovery_code_length');
}

/** The 16 bytes of a recovery code text. */
export function parseRecoveryCode(text: unknown): Uint8Array<ArrayBuffer> {
  return parseCodeText(text, 'recovery_code_length');
}

/** The b64u ID under which the hub finds the recovery slot (it never sees the code). */
export async function recoveryId(code: Uint8Array): Promise<string> {
  return b64uEncode(await recoveryIdRaw(code));
}

/** `P || "recovery-proof\0" || user_id || "\0" || challenge`. */
export function proofText(userId: unknown, challenge: Uint8Array): Uint8Array<ArrayBuffer> {
  if (typeof userId !== 'string' || userId === '') fail('header_invalid');
  if (!isPrintableAscii(userId)) fail('non_ascii');
  return concat(label(LABEL_RECOVERY_PROOF), utf8(`${userId}\0`), requireBytes(challenge, CHALLENGE_BYTES));
}

/** The b64u proof signature of the unlocked identity. */
export async function signProof(identitySig: CryptoKey, userId: string, challenge: Uint8Array): Promise<string> {
  return b64uEncode(await ed25519Sign(identitySig, proofText(userId, challenge)));
}

/** Checks the proof against the registered identity (`signature_invalid`). */
export async function verifyProof(
  identitySigPublic: Uint8Array,
  userId: string,
  challenge: Uint8Array,
  proof: unknown,
): Promise<void> {
  await ed25519VerifyB64u(identitySigPublic, proof, proofText(userId, challenge));
}

/** The body part `{recovery_id, bundle_ct}` of a recovery slot. */
export interface RecoverySlot {
  readonly recovery_id: string;
  readonly bundle_ct: string;
}

/** The nonce that marks the unset recovery slot of `userId` (only the identity can derive it). */
async function unsetNonce(userId: string, identity: Identity): Promise<Uint8Array<ArrayBuffer>> {
  const seed = identity.revealPrivate((_kem, sig) => sig);
  const nonce = await hkdf(seed, concat(label(RECOVERY_UNSET), utf8(userId)));
  seed.fill(0);
  return nonce.slice(0, NONCE_BYTES);
}

/** A recovery slot that no code opens (no code was shown): random dropped code, zero plaintext, marked nonce. */
export async function unsetRecoverySlot(userId: string, identity: Identity): Promise<RecoverySlot> {
  const code = generateRecoveryCode();
  const kek = await kekFromRecoveryCode(code);
  const id = await recoveryId(code);
  code.fill(0);
  const zeros = bundlePlaintext(identity).fill(0);
  const sealed = await sealBlob(kek, zeros, bundleAad(userId, RECOVERY_SLOT), await unsetNonce(userId, identity));
  return { recovery_id: id, bundle_ct: b64uEncode(sealed) };
}

/** True if `bundleCt` is the unset recovery slot of `userId` and `identity` (no recovery code exists). */
export async function isUnsetRecoverySlot(userId: string, identity: Identity, bundleCt: string): Promise<boolean> {
  const sealed = b64uDecode(bundleCt);
  const nonce = await unsetNonce(userId, identity);
  return equalBytes(sealed.subarray(0, NONCE_BYTES), nonce);
}
