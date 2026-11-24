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
//
// Pairing from another computer (ADR-067, 11-agent-access.md sections 3 and 5.4):
// - `agentCode`: `crockford32(SHA-256(P || "agent-code\0" || canonical({"fp","mode":"remote","nonce","origin",
//   "pairing_id"})))[:10]` with the hub's 16-byte nonce; the user types it, the page never shows it.
// - `sealHandover`/`openHandover`: `canonical({"k","r"})` (platform root key of the build, `r`) HPKE-sealed to the
//   agent's X25519 key with `info = P || "agent-handover\0" || ids`; the hub only relays it.
// - `returnCode`: `crockford32(HKDF(r, P || "agent-return\0" || ids))[:8]`, shown by the page after the
//   confirmation; the agent accepts the handover only when the user gives it this code.
// - `parseCode`: the typed form of a code (lower case, spaces and dashes; `I`/`L` read as `1`, `O` as `0`);
//   `formatCode` shows it in two groups.
//
// An account of an agent alone (rule E-27, KOR-62, 11-agent-access.md sections 3 and 5.5): the agent tool creates the
// user's identity itself, with no passkey, and proves possession with signatures; the proof names its method
// (`PROOF_METHODS`, today only `agent-signature-v1`; `checkProofMethod`).
// - `accountText`: `P || "agent-account\0" || canonical({"agent","agent_pub":{kem,sig},"invite_id","method",
//   "pub":{kem,sig},"user"})`, signed by `sk_inv`, the new identity and the new agent key (`verifyText`).
// - `recoverText`: `P || "agent-recover\0" || canonical({"agent","agent_pub":{kem,sig},"challenge","method","user"})`
//   with the 32-byte challenge of `recovery/begin`, signed by the recovered identity and the new agent key.
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
/** Longest personal message of a request for access (rule E-28, KOR-64), in characters. */
export const MESSAGE_TEXT_MAX = 500;
export const MODE_REMOTE = 'remote';
export const NONCE_BYTES = 16;
export const AGENT_CODE_LENGTH = 10;
export const RETURN_CODE_LENGTH = 8;
export const METHOD_AGENT_SIGNATURE = 'agent-signature-v1';
export const PROOF_METHODS: readonly string[] = [METHOD_AGENT_SIGNATURE];
export const CHALLENGE_BYTES = 32;
export const CROCKFORD32 = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const CODE_ALIASES: Readonly<Record<string, string>> = { I: '1', L: '1', O: '0' };
const HANDOVER_KEYS = ['k', 'r'];
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

function isRuledText(text: unknown, max: number): text is string {
  return (
    typeof text === 'string' &&
    [...text].length >= 1 &&
    [...text].length <= max &&
    text === text.trim() &&
    !REFUSED.test(text)
  );
}

/** Whether `text` may be the name or host of a label (module comment). */
export function isLabelText(text: unknown): text is string {
  return isRuledText(text, LABEL_TEXT_MAX);
}

/**
 * Whether `text` may be the personal message of a request for access (rule E-28, KOR-64): 1–500 characters under the
 * label rules. It is shown only as plain text, quoted as the requester's words.
 */
export function isMessageText(text: unknown): text is string {
  return isRuledText(text, MESSAGE_TEXT_MAX);
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

/** Crockford base32 of `raw` (big-endian bits, 5 per character, the last one padded with zeros). */
export function crockford32(raw: Uint8Array): string {
  const bits = [...raw].map((byte) => byte.toString(2).padStart(8, '0')).join('');
  let text = '';
  for (let at = 0; at < bits.length; at += 5) text += CROCKFORD32[parseInt(bits.slice(at, at + 5).padEnd(5, '0'), 2)];
  return text;
}

/** The SHA-256 input of the agent code. */
export function codeInput(pairingId: string, fp: string, nonce: Uint8Array, origin: string): Uint8Array<ArrayBuffer> {
  const fields = {
    fp,
    mode: MODE_REMOTE,
    nonce: b64uEncode(requireRaw(nonce, NONCE_BYTES)),
    origin,
    pairing_id: pairingId,
  };
  return concat(L.label(L.LABEL_AGENT_CODE), canonical(fields));
}

/** The 10-character agent code of a remote pairing (the user types it on the page). */
export async function agentCode(pairingId: string, fp: string, nonce: Uint8Array, origin: string): Promise<string> {
  return crockford32(await sha256(codeInput(pairingId, fp, nonce, origin))).slice(0, AGENT_CODE_LENGTH);
}

/** The HKDF `info` of the return code. */
export function returnInfo(agentId: string, userId: string): Uint8Array<ArrayBuffer> {
  return concat(L.label(L.LABEL_AGENT_RETURN), ids(agentId, userId));
}

/** The 8-character return code the page shows after the confirmation. */
export async function returnCode(secret: Raw, agentId: string, userId: string): Promise<string> {
  return crockford32(await hkdf(secretOf(secret), returnInfo(agentId, userId))).slice(0, RETURN_CODE_LENGTH);
}

/** The canonical code of a typed text (module comment); exactly `length` characters, else `header_invalid`. */
export function parseCode(text: unknown, length: number): string {
  if (typeof text !== 'string') fail('header_invalid');
  const code = [...text.toUpperCase().replaceAll(' ', '').replaceAll('-', '')]
    .map((char) => CODE_ALIASES[char] ?? char)
    .join('');
  if (code.length !== length || [...code].some((char) => !CROCKFORD32.includes(char))) fail('header_invalid');
  return code;
}

/** The typed code, or null when it is not a well-formed code of `length` characters. */
export function tryParseCode(text: string, length: number): string | null {
  try {
    return parseCode(text, length);
  } catch {
    return null;
  }
}

/** Two groups separated by a dash (`XXXXX-XXXXX`, `XXXX-XXXX`). */
export function formatCode(code: string): string {
  const half = Math.floor(code.length / 2);
  return `${code.slice(0, half)}-${code.slice(half)}`;
}

/** The HPKE `info` of the handover sealed to the agent. */
export function handoverInfo(agentId: string, userId: string): Uint8Array<ArrayBuffer> {
  return concat(L.label(L.LABEL_AGENT_HANDOVER), ids(agentId, userId));
}

/** `canonical({"k","r"})`: the plaintext of `handover_ct`. */
export function handoverPlaintext(secret: Raw, rootKey: string): Uint8Array<ArrayBuffer> {
  const r = secretOf(secret).reveal((raw) => b64uEncode(raw));
  return canonical({ k: b64uEncode(b64uDecode(rootKey, RAW_KEY_BYTES)), r });
}

/** `handover_ct`: `r` and the platform root key sealed to the agent (`ephemeral` only for test vectors). */
export async function sealHandover(
  agentKemPub: Uint8Array,
  agentId: string,
  userId: string,
  secret: Raw,
  rootKey: string,
  ephemeral?: Uint8Array,
): Promise<string> {
  const plaintext = handoverPlaintext(secret, rootKey);
  return b64uEncode(await hpke.seal(agentKemPub, handoverInfo(agentId, userId), plaintext, ephemeral));
}

/** The inverse of `sealHandover`: `r` and the platform root key; the plaintext must be exactly canonical. */
export async function openHandover(
  agentKem: KeyPair,
  agentId: string,
  userId: string,
  sealed: string,
): Promise<{ readonly secret: Uint8Array<ArrayBuffer>; readonly rootKey: string }> {
  const plaintext = await hpke.open(agentKem, handoverInfo(agentId, userId), b64uDecode(sealed));
  const value = requireKeys(parseJson(plaintext), HANDOVER_KEYS);
  const secret = b64uDecode(value.r, SECRET_BYTES);
  const rootKey = value.k as string;
  if (!equalBytes(handoverPlaintext(secret, rootKey), plaintext)) fail('header_invalid');
  return { secret, rootKey };
}

/** The fields of `accountText` (raw 32-byte public keys). */
export interface AccountFields {
  readonly agentId: string;
  readonly agentKem: Uint8Array;
  readonly agentSig: Uint8Array;
  readonly inviteId: string;
  readonly method: unknown;
  readonly userId: string;
  readonly userKem: Uint8Array;
  readonly userSig: Uint8Array;
}

/** The fields of `recoverText` (raw 32-byte public keys and challenge). */
export interface RecoverFields {
  readonly agentId: string;
  readonly agentKem: Uint8Array;
  readonly agentSig: Uint8Array;
  readonly challenge: Uint8Array;
  readonly method: unknown;
  readonly userId: string;
}

const publicKeys = (kemPub: Uint8Array, sigPub: Uint8Array) => ({
  kem: b64uEncode(requireRaw(kemPub, RAW_KEY_BYTES)),
  sig: b64uEncode(requireRaw(sigPub, RAW_KEY_BYTES)),
});

/** A proof method of `PROOF_METHODS` (`header_invalid` otherwise). */
export function checkProofMethod(method: unknown): string {
  if (!(PROOF_METHODS as readonly unknown[]).includes(method)) fail('header_invalid');
  return method as string;
}

/** The text the invitation key, the new identity and the new agent key sign for an account of an agent alone. */
export function accountText(f: AccountFields): Uint8Array<ArrayBuffer> {
  const fields = {
    agent: f.agentId,
    agent_pub: publicKeys(f.agentKem, f.agentSig),
    invite_id: f.inviteId,
    method: checkProofMethod(f.method),
    pub: publicKeys(f.userKem, f.userSig),
    user: f.userId,
  };
  return concat(L.label(L.LABEL_AGENT_ACCOUNT), canonical(fields));
}

/** The text the recovered identity and the new agent key sign to make the new agent the main orchestrator. */
export function recoverText(f: RecoverFields): Uint8Array<ArrayBuffer> {
  const fields = {
    agent: f.agentId,
    agent_pub: publicKeys(f.agentKem, f.agentSig),
    challenge: b64uEncode(requireRaw(f.challenge, CHALLENGE_BYTES)),
    method: checkProofMethod(f.method),
    user: f.userId,
  };
  return concat(L.label(L.LABEL_AGENT_RECOVER), canonical(fields));
}

/** `signature_invalid` unless `signature` (b64u) is the signature of `sigPub` over `text`. */
export function verifyText(sigPub: Uint8Array, signature: unknown, text: Uint8Array): Promise<void> {
  return ed25519VerifyB64u(sigPub, signature, text);
}
