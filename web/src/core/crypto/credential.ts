// Credential records (04-crypto.md section 4.4), twin of `agent_hub.crypto.credential`. Header key set exactly
// `HEADER_KEYS`; `ct = AES-GCM(k_cred, body, AAD = canonical(header))` with `k_cred = HKDF(CK_e, salt = header.salt,
// info = P || "credential")`; `sig = Ed25519_author(P || "credential-sig\0" || canonical(header) || ct_raw)`.
import { fail } from './errors';
import * as F from './fields';
import { credentialKey, SALT_BYTES } from './kdf';
import { LABEL_CREDENTIAL_SIG } from './labels';
import { Secret } from './secret';
import * as signed from './signed';

export const VERSION = 1;
export const ALG = 'A256GCM-HKDF';
export const HEADER_KEYS = ['v', 'alg', 'id', 'owner', 'kind', 'version', 'epoch', 'author', 'ts', 'salt', 'nonce'];
export const KINDS = ['github', 'anthropic_api_key', 'claude_subscription', 'mailbox'];
/** No own limit in the specification; bounded by the signed request body limit of 03-api.md section 1. */
export const MAX_CT_BYTES = 1024 * 1024;

/** Exact key set, `v`, `alg`, `kind` from the closed list and value formats. */
export function checkHeader(value: unknown): F.Fields {
  const header = F.headerDict(value, HEADER_KEYS);
  F.literal(header.v, VERSION);
  F.literal(header.alg, ALG);
  F.uuid(header.id);
  F.uuid(header.owner);
  if (!KINDS.includes(header.kind as string)) fail('header_invalid');
  F.requireInt(header.version, 1);
  F.requireInt(header.epoch, 1);
  F.uuid(header.author);
  F.time(header.ts);
  F.b64uBytes(header.salt, SALT_BYTES);
  F.b64uBytes(header.nonce, 12);
  return header;
}

export const FORMAT: signed.Format = {
  checkHeader,
  deriveKey: credentialKey,
  sigLabel: LABEL_CREDENTIAL_SIG,
  maxCt: () => MAX_CT_BYTES,
};

export interface HeaderInput {
  id: string;
  owner: string;
  kind: string;
  version: number;
  epoch: number;
  author: string;
  ts: string;
}

/** The checked header fields without `salt` and `nonce` (`sealCredential` draws those). */
export function newHeader(input: HeaderInput): F.Fields {
  return signed.checkFields(FORMAT, { v: VERSION, alg: ALG, ...input });
}

/** Encrypts the body JSON under `CK_e` and signs as the author (owner or acting device); fresh salt and nonce. */
export function sealCredential(
  credKey: Secret,
  fields: unknown,
  body: Uint8Array,
  author: CryptoKey,
  fixedSaltNonce?: readonly [Uint8Array, Uint8Array],
): Promise<signed.Sealed> {
  return signed.seal(FORMAT, credKey, fields, body, author, fixedSaltNonce);
}

/** Structure, header, size and signature without any key; returns the checked header. */
export async function verifyCredential(record: unknown, authorSigPublic: Uint8Array): Promise<F.Fields> {
  return (await signed.verify(FORMAT, record, authorSigPublic)).header;
}

/** `verifyCredential`, then decrypt. */
export function openCredential(credKey: Secret, record: unknown, authorSigPublic: Uint8Array): Promise<Uint8Array> {
  return signed.open(FORMAT, credKey, record, authorSigPublic);
}
