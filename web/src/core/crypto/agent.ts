// Delegated agent access (11-agent-access.md section 3, ADR-056), twin of `agent_hub.crypto.agent`.
//
// - `fingerprint`: `b64u(SHA-256(P || "agent-key\0" || kem_pub || sig_pub)[:16])`, carried in the fragment of the
//   pairing link; the browser compares it with the keys the hub returns.
// - `popText`/`verifyPop`: the agent's proof of possession (the browser never signs one; the vectors name it).
// - The handover secret `r` (32 bytes, drawn by the confirming browser, handed to the agent only over the loopback)
//   yields the bundle key `k` and the claim; the hub stores only `claimHash(claim)`.
// - `sealIdentity`/`openIdentity`: the identity bundle plaintext (04-crypto.md section 4.1) first under `k`
//   (`nonce || AES-GCM(k, plaintext, AAD = info)`), then HPKE-sealed to the agent's X25519 key with the same `info`.
// - `sealLabel`/`openLabel`: `{"host","name"}` (body JSON) HPKE-sealed to the user's identity X25519 key; each text
//   1–64 characters, no leading or trailing space, none of the Unicode categories Cc Cf Zl Zp Cs Co Cn.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, equalBytes } from '../encoding/bytes';
import { bodyJson, canonical, parseJson } from '../encoding/json';
import * as aead from './aead';
import { bundlePlaintext, parseBundlePlaintext } from './bundle';
import { fail } from './errors';
import { requireKeys } from './fields';
import * as hpke from './hpke';
import { hkdf, hkdfAesKey } from './kdf';
import { ed25519VerifyB64u, type Identity, type KeyPair, RAW_KEY_BYTES } from './keys';
import * as L from './labels';
import { Secret } from './secret';
import { sha256 } from './subtle';

export const FINGERPRINT_BYTES = 16;
export const SECRET_BYTES = 32;
export const LABEL_TEXT_MAX = 64;
const LABEL_KEYS = ['host', 'name'];
// Controls, format characters (bidi overrides, zero-width), line/paragraph separators, surrogates, private use and
// unassigned code points.
const REFUSED = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}\p{Co}\p{Cn}]/u;

/** The label an agent gives itself: its name and the computer's name (as the agent states them). */
export interface AgentLabel {
  readonly host: string;
  readonly name: string;
}

type Raw = Uint8Array | Secret;

function requireRaw(raw: Uint8Array, length: number): Uint8Array {
  if (raw.length !== length) fail('header_invalid');
  return raw;
}

function secretOf(secret: Raw): Secret {
  if (secret.length !== SECRET_BYTES) fail('header_invalid');
  return secret instanceof Secret ? secret : Secret.of(secret);
}

/** The 22-character fingerprint of an agent's public keys. */
export async function fingerprint(kemPub: Uint8Array, sigPub: Uint8Array): Promise<string> {
  const keys = concat(requireRaw(kemPub, RAW_KEY_BYTES), requireRaw(sigPub, RAW_KEY_BYTES));
  const digest = await sha256(concat(L.label(L.LABEL_AGENT_KEY), keys));
  return b64uEncode(digest.subarray(0, FINGERPRINT_BYTES));
}

/** The text of the agent's proof of possession. */
export function popText(pairingId: string, kemPub: Uint8Array, sigPub: Uint8Array): Uint8Array<ArrayBuffer> {
  const fields = {
    kem: b64uEncode(requireRaw(kemPub, RAW_KEY_BYTES)),
    pairing_id: pairingId,
    sig: b64uEncode(requireRaw(sigPub, RAW_KEY_BYTES)),
  };
  return concat(L.label(L.LABEL_AGENT_PAIR), canonical(fields));
}

/** `signature_invalid` unless `pop` is the signature of `sigPub` over `popText`. */
export function verifyPop(pairingId: string, kemPub: Uint8Array, sigPub: Uint8Array, pop: unknown): Promise<void> {
  return ed25519VerifyB64u(sigPub, pop, popText(pairingId, kemPub, sigPub));
}

const ids = (agentId: string, userId: string) => canonical({ agent: agentId, user: userId });

/** The HPKE `info` (and inner AAD) of the identity sealed to the agent. */
export function identityInfo(agentId: string, userId: string): Uint8Array<ArrayBuffer> {
  return concat(L.label(L.LABEL_AGENT_BUNDLE), ids(agentId, userId));
}

/** The HPKE `info` of the label sealed to the user. */
export function labelInfo(agentId: string, userId: string): Uint8Array<ArrayBuffer> {
  return concat(L.label(L.LABEL_AGENT_LABEL), ids(agentId, userId));
}

/** `k = HKDF(r, P || "agent-bundle-key\0" || ids)` as a non-extractable AES-256-GCM key. */
export function bundleKey(secret: Raw, agentId: string, userId: string): Promise<CryptoKey> {
  return hkdfAesKey(secretOf(secret), concat(L.label(L.LABEL_AGENT_BUNDLE_KEY), ids(agentId, userId)));
}

/** The claim the agent sends: `HKDF(r, P || "agent-claim\0" || ids)`. */
export function claim(secret: Raw, agentId: string, userId: string): Promise<Uint8Array<ArrayBuffer>> {
  return hkdf(secretOf(secret), concat(L.label(L.LABEL_AGENT_CLAIM), ids(agentId, userId)));
}

/** `SHA-256(P || "agent-claim-hash\0" || claim)` of a 32-byte claim (what the hub stores). */
export function claimHash(claimRaw: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  return sha256(concat(L.label(L.LABEL_AGENT_CLAIM_HASH), requireRaw(claimRaw, SECRET_BYTES)));
}

/** Fixed values of a seal (test vectors only). */
export interface Fixed {
  readonly nonce?: Uint8Array;
  readonly ephemeral?: Uint8Array;
}

/** The user's identity under `k` and sealed to the agent's X25519 key (b64u). */
export async function sealIdentity(
  agentKemPub: Uint8Array,
  secret: Raw,
  agentId: string,
  userId: string,
  identity: Identity,
  fixed: Fixed = {},
): Promise<string> {
  const info = identityInfo(agentId, userId);
  const key = await bundleKey(secret, agentId, userId);
  const inner = await aead.sealBlob(key, bundlePlaintext(identity), info, fixed.nonce);
  return b64uEncode(await hpke.seal(agentKemPub, info, inner, fixed.ephemeral));
}

/** The inverse of `sealIdentity` (`hpke_short`, `decrypt_failed`, `header_invalid`). */
export async function openIdentity(
  agentKem: KeyPair,
  secret: Raw,
  agentId: string,
  userId: string,
  sealed: string,
): Promise<Identity> {
  const info = identityInfo(agentId, userId);
  const inner = await hpke.open(agentKem, info, b64uDecode(sealed));
  return parseBundlePlaintext(await aead.openBlob(await bundleKey(secret, agentId, userId), inner, info));
}

/** Whether `text` may be the name or host of a label (module comment). */
export function isLabelText(text: unknown): text is string {
  return (
    typeof text === 'string' &&
    [...text].length >= 1 &&
    [...text].length <= LABEL_TEXT_MAX &&
    text === text.trim() &&
    !REFUSED.test(text)
  );
}

/** Exactly `{"host","name"}` with valid texts (`header_invalid`). */
export function checkLabel(value: unknown): AgentLabel {
  const label = requireKeys(value, LABEL_KEYS);
  if (!LABEL_KEYS.every((key) => isLabelText(label[key]))) fail('header_invalid');
  return { host: label.host as string, name: label.name as string };
}

/** The label sealed to the user's identity X25519 key (b64u). */
export async function sealLabel(
  userKemPub: Uint8Array,
  agentId: string,
  userId: string,
  label: unknown,
  ephemeral?: Uint8Array,
): Promise<string> {
  const body = bodyJson(checkLabel(label));
  return b64uEncode(await hpke.seal(userKemPub, labelInfo(agentId, userId), body, ephemeral));
}

/** The inverse of `sealLabel`; the plaintext must be the exact body JSON of a valid label. */
export async function openLabel(
  userKem: KeyPair,
  agentId: string,
  userId: string,
  sealed: string,
): Promise<AgentLabel> {
  const plaintext = await hpke.open(userKem, labelInfo(agentId, userId), b64uDecode(sealed));
  const label = checkLabel(parseJson(plaintext));
  if (!equalBytes(bodyJson(label), plaintext)) fail('header_invalid');
  return label;
}
