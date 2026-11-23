// Recovery codes (04-crypto.md sections 3, 4.1 and 5.5), twin of `agent_hub.crypto.recovery`.
// `recovery_id = b64u(HKDF(code16, info=P||"recovery-id")[:16])`, `KEK_rec` in `kdf.ts`, and the proof
// `Ed25519_identity(P || "recovery-proof\0" || user_id || "\0" || challenge)` over the raw 32-byte challenge of
// `POST /v1/recovery/begin`. The code text has the form of invitation codes; a wrong length is `recovery_code_length`.
import { b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { isPrintableAscii } from '../encoding/json';
import { fail } from './errors';
import { requireBytes } from './fields';
import { formatCodeText, parseCodeText } from './invite';
import { CODE_BYTES, recoveryIdRaw } from './kdf';
import { ed25519Sign, ed25519VerifyB64u } from './keys';
import { label, LABEL_RECOVERY_PROOF } from './labels';
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
