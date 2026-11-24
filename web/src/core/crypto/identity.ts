// Identity proof and account code of a user (04-crypto.md sections 3 and 5.13, ADR-060), twin of
// `agent_hub.crypto.identity`.
//
// Identity proof: `id_sig = Ed25519_{sk_inv}(P || "invite-identity\0" || canonical({"pub": pub, "user": user}))`, made
// by the registering browser from the code of the platform invitation it registers with. Together with the
// root-signed `user_invite {pk_inv}` of the platform log it proves, without trusting the hub, which identity
// `pub = {kem, sig}` the holder of the code registered for `user`. A user registered through a platform invitation has
// the ID derived from that invitation (`invitedUserId`: UUIDv7 with the milliseconds of the root-signed `user_invite`
// entry and the random bits `SHA-256(P || "user-id\0" || invite_id)[:10]`), so a proof made with one code names
// exactly one account.
//
// Account code: `base32(SHA-256(P || "account-code\0" || user || "\0" || pub.sig || pub.kem))[:12]` (RFC 4648
// alphabet, uppercase), shown as `XXXX-XXXX-XXXX`. The parser strips `-` and spaces, uppercases and reads the
// look-alikes `0`, `1`, `8` as `O`, `I`, `B`; a wrong length or a character outside the alphabet is
// `account_code_invalid`.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { uuid7 } from '../encoding/ids';
import { canonical } from '../encoding/json';
import { parseTime } from '../encoding/time';
import { CryptoError, fail } from './errors';
import * as F from './fields';
import { ed25519Sign, ed25519VerifyB64u, RAW_KEY_BYTES } from './keys';
import { label, LABEL_ACCOUNT_CODE, LABEL_INVITE_IDENTITY, LABEL_USER_ID } from './labels';
import { sha256 } from './subtle';

export const ACCOUNT_CODE_LENGTH = 12;
export const INVITE_ID_BYTES = 16;
const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const CODE_RE = /^[A-Z2-7]{12}$/;
const LOOKALIKES: Readonly<Record<string, string>> = { '0': 'O', '1': 'I', '8': 'B' };

/** The identity keys of a user: X25519 `kem` and Ed25519 `sig`, b64u. */
export interface PublicIdentity {
  readonly kem: string;
  readonly sig: string;
}

function pubOf(pub: unknown): PublicIdentity {
  const found = F.requireKeys(pub, ['kem', 'sig']);
  return { kem: F.b64uBytes(found.kem, RAW_KEY_BYTES), sig: F.b64uBytes(found.sig, RAW_KEY_BYTES) };
}

/** `P || "invite-identity\0" || canonical({"pub": pub, "user": user})`. */
export function identityText(user: unknown, pub: unknown): Uint8Array<ArrayBuffer> {
  const keys = pubOf(pub);
  return concat(label(LABEL_INVITE_IDENTITY), canonical({ pub: keys, user: F.uuid(user) }));
}

/** The b64u `id_sig` by `sk_inv` of the code. */
export async function signIdentity(signing: CryptoKey, user: string, pub: PublicIdentity): Promise<string> {
  return b64uEncode(await ed25519Sign(signing, identityText(user, pub)));
}

/** True if `idSig` is a valid signature of the identity text under `pkInv` (b64u). */
export async function identityValid(pkInv: string, user: unknown, pub: unknown, idSig: unknown): Promise<boolean> {
  try {
    await ed25519VerifyB64u(b64uDecode(pkInv, RAW_KEY_BYTES), idSig, identityText(user, pub));
  } catch (error) {
    if (error instanceof CryptoError) return false;
    throw error;
  }
  return true;
}

/**
 * The user ID of the account registered through the platform invitation `inviteId` whose `user_invite` entry has
 * the time `inviteTs`.
 */
export async function invitedUserId(inviteId: string, inviteTs: string): Promise<string> {
  const digest = await sha256(concat(label(LABEL_USER_ID), b64uDecode(inviteId, INVITE_ID_BYTES)));
  return uuid7(parseTime(inviteTs), digest.slice(0, 10));
}

/** The 12-character account code (without dashes). */
export async function accountCode(user: unknown, pub: unknown): Promise<string> {
  const keys = pubOf(pub);
  const digest = await sha256(
    concat(
      label(LABEL_ACCOUNT_CODE),
      utf8(`${F.uuid(user)}\0`),
      b64uDecode(keys.sig, RAW_KEY_BYTES),
      b64uDecode(keys.kem, RAW_KEY_BYTES),
    ),
  );
  let bits = '';
  for (const byte of digest.slice(0, 8)) bits += byte.toString(2).padStart(8, '0');
  let code = '';
  for (let at = 0; at < ACCOUNT_CODE_LENGTH * 5; at += 5) code += BASE32[parseInt(bits.slice(at, at + 5), 2)];
  return code;
}

/** `XXXX-XXXX-XXXX`. */
export function formatAccountCode(code: string): string {
  return [0, 4, 8].map((at) => code.slice(at, at + 4)).join('-');
}

/** The 12 characters of a typed code (`account_code_invalid` otherwise). */
export function parseAccountCode(text: unknown): string {
  if (typeof text !== 'string') fail('account_code_invalid');
  const code = text
    .replace(/[- ]/g, '')
    .toUpperCase()
    .replace(/[018]/g, (char) => LOOKALIKES[char]!);
  if (!CODE_RE.test(code)) fail('account_code_invalid');
  return code;
}
