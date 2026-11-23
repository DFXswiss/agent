// Device requests and responses (04-crypto.md section 4.5), twin of `agent_hub.crypto.request`.
// Envelope `{"header", "ct": b64u(enc || ct), "sig"}`: HPKE single shot to the recipient's X25519 key (the device for
// requests, the user identity for responses) with `info = P || "request-wrap\0" || canonical(header)` and empty AAD;
// `sig = Ed25519_sender(P || "request-sig\0" || canonical(header) || ct_raw)`.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat } from '../encoding/bytes';
import { canonical } from '../encoding/json';
import { fail } from './errors';
import * as F from './fields';
import * as hpke from './hpke';
import { ed25519Sign, ed25519VerifyB64u, type KeyPair } from './keys';
import { label, LABEL_REQUEST_SIG, LABEL_REQUEST_WRAP } from './labels';

export const VERSION = 1;
export const ENC_ALG = 'HPKE-X25519-A256GCM';
export const REQUEST_KEYS = ['v', 'id', 'user', 'device', 'kind', 'follows', 'ts', 'enc_alg'];
export const RESPONSE_KEYS = ['v', 'id', 'user', 'device', 'kind', 'in_reply_to', 'seq', 'final', 'ts', 'enc_alg'];
const ENVELOPE_KEYS = ['header', 'ct', 'sig'];
/** Bounded by the signed request body limit of 03-api.md section 1. */
export const MAX_CT_BYTES = 1024 * 1024;

export interface Envelope {
  header: F.Fields;
  ct: string;
  sig: string;
}

function common(header: F.Fields): void {
  F.literal(header.v, VERSION);
  F.literal(header.enc_alg, ENC_ALG);
  F.uuid(header.id);
  F.uuid(header.user);
  F.uuid(header.device);
  F.kind(header.kind);
  F.time(header.ts);
}

/** Exact request key set and value formats (`follows` is a request ID or `""`). */
export function checkRequestHeader(value: unknown): F.Fields {
  const header = F.headerDict(value, REQUEST_KEYS);
  common(header);
  F.uuidOrEmpty(header.follows);
  return header;
}

/** Exact response key set and value formats (`seq` from 1, `final` boolean). */
export function checkResponseHeader(value: unknown): F.Fields {
  const header = F.headerDict(value, RESPONSE_KEYS);
  common(header);
  F.uuid(header.in_reply_to);
  F.requireInt(header.seq, 1);
  if (typeof header.final !== 'boolean') fail('header_invalid');
  return header;
}

/** The HPKE `info`: `P || "request-wrap\0" || canonical(header)`. */
export function wrapInfo(header: F.Fields): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_REQUEST_WRAP), canonical(header));
}

/** `P || "request-sig\0" || canonical(header) || ct_raw`. */
export function signatureText(header: F.Fields, ctRaw: Uint8Array): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_REQUEST_SIG), canonical(header), ctRaw);
}

async function sealWith(
  header: F.Fields,
  recipientKemPublic: Uint8Array,
  plaintext: Uint8Array,
  sender: CryptoKey,
  ephemeral: Uint8Array | undefined,
): Promise<Envelope> {
  const ct = await hpke.seal(recipientKemPublic, wrapInfo(header), plaintext, ephemeral);
  if (ct.length > MAX_CT_BYTES) fail('size_exceeded');
  return { header, ct: b64uEncode(ct), sig: b64uEncode(await ed25519Sign(sender, signatureText(header, ct))) };
}

async function verifyWith(envelope: unknown, response: boolean, senderSigPublic: Uint8Array) {
  const data = F.requireKeys(envelope, ENVELOPE_KEYS);
  const header = response ? checkResponseHeader(data.header) : checkRequestHeader(data.header);
  const ct = b64uDecode(data.ct);
  if (ct.length > MAX_CT_BYTES) fail('size_exceeded');
  await ed25519VerifyB64u(senderSigPublic, data.sig, signatureText(header, ct));
  return { header, ct };
}

/** Encrypts a request body to the device and signs it with the user identity; `ephemeral` for vectors only. */
export function sealRequest(
  header: unknown,
  deviceKemPublic: Uint8Array,
  plaintext: Uint8Array,
  user: CryptoKey,
  ephemeral?: Uint8Array,
): Promise<Envelope> {
  return sealWith(checkRequestHeader(header), deviceKemPublic, plaintext, user, ephemeral);
}

/** Structure, header, size and the user's signature (what the hub checks); returns the header. */
export async function verifyRequest(envelope: unknown, userSigPublic: Uint8Array): Promise<F.Fields> {
  return (await verifyWith(envelope, false, userSigPublic)).header;
}

/** `verifyRequest`, then decrypt with the device X25519 key. */
export async function openRequest(device: KeyPair, envelope: unknown, userSigPublic: Uint8Array): Promise<Uint8Array> {
  const { header, ct } = await verifyWith(envelope, false, userSigPublic);
  return hpke.open(device, wrapInfo(header), ct);
}

/** Encrypts a response body to the user identity and signs it with the device key. */
export function sealResponse(
  header: unknown,
  userKemPublic: Uint8Array,
  plaintext: Uint8Array,
  device: CryptoKey,
  ephemeral?: Uint8Array,
): Promise<Envelope> {
  return sealWith(checkResponseHeader(header), userKemPublic, plaintext, device, ephemeral);
}

/** Structure, header, size and the device's signature; returns the header. */
export async function verifyResponse(envelope: unknown, deviceSigPublic: Uint8Array): Promise<F.Fields> {
  return (await verifyWith(envelope, true, deviceSigPublic)).header;
}

/** `verifyResponse`, then decrypt with the user identity X25519 key. */
export async function openResponse(user: KeyPair, envelope: unknown, deviceSigPublic: Uint8Array): Promise<Uint8Array> {
  const { header, ct } = await verifyWith(envelope, true, deviceSigPublic);
  return hpke.open(user, wrapInfo(header), ct);
}
