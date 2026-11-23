// Enrollment text of platform devices (04-crypto.md section 3; body of `POST /v1/device/enroll`), twin of
// `agent_hub.crypto.enroll`. Text `P || "enroll\0" || canonical(body without enroll_sig and inv_sig)`; `enroll_sig`
// is the Ed25519 signature of the device identity (`body.pub.sig`), `inv_sig` the signature of `sk_inv`, both over
// this text. `device_certify` copies both so that browsers can check them.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, equalBytes } from '../encoding/bytes';
import { canonical, isPlainObject } from '../encoding/json';
import { fail } from './errors';
import * as F from './fields';
import { ed25519Sign, ed25519VerifyB64u, type KeyPair, RAW_KEY_BYTES } from './keys';
import { label, LABEL_ENROLL } from './labels';

export const SIGNATURE_FIELDS = ['enroll_sig', 'inv_sig'];
const PUB_KEYS = ['kem', 'sig'];

function unsigned(body: unknown): F.Fields {
  if (!isPlainObject(body)) fail('header_invalid');
  return Object.fromEntries(Object.entries(body).filter(([key]) => !SIGNATURE_FIELDS.includes(key)));
}

/** The signed text; the signature fields are left out whether present or not. */
export function enrollmentText(body: unknown): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_ENROLL), canonical(unsigned(body)));
}

function deviceSigPublic(body: F.Fields): Uint8Array<ArrayBuffer> {
  const pub = F.headerDict(body.pub, PUB_KEYS);
  F.b64uBytes(pub.kem, RAW_KEY_BYTES);
  return b64uDecode(pub.sig, RAW_KEY_BYTES);
}

/** The body with `enroll_sig` (device identity) and `inv_sig` (`sk_inv`) over the enrollment text. */
export async function signEnrollment(body: unknown, device: KeyPair, invite: KeyPair): Promise<F.Fields> {
  const plain = unsigned(body);
  if (!equalBytes(deviceSigPublic(plain), device.publicKey)) fail('header_invalid');
  const text = enrollmentText(plain);
  return {
    ...plain,
    enroll_sig: b64uEncode(await ed25519Sign(device.privateKey, text)),
    inv_sig: b64uEncode(await ed25519Sign(invite.privateKey, text)),
  };
}

/** Checks `enroll_sig` under `body.pub.sig` and `inv_sig` under `pk_inv` (`signature_invalid`). */
export async function verifyEnrollment(body: unknown, pkInv: Uint8Array): Promise<void> {
  const plain = unsigned(body);
  const devicePublic = deviceSigPublic(plain);
  const text = enrollmentText(plain);
  const signed = body as F.Fields;
  await ed25519VerifyB64u(devicePublic, signed.enroll_sig, text);
  await ed25519VerifyB64u(pkInv, signed.inv_sig, text);
}
