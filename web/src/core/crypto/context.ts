// Context checkpoints and pull-request tags (04-crypto.md sections 3 and 4.11, ADR-072), twin of
// `agent_hub.crypto.context` (vectors `contracts/vectors/context.json`). The browser holds the workgroup key, so it
// can compute the opaque tag of a pull request (`GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`) and verify and
// open a checkpoint like the runner does:
//
// - `k_ctx = HKDF(WK_e, salt=manifest.salt (32 B), info=P || "context\0" || object_id)`,
// - chunk `i` = `nonce(12) || AES-256-GCM(k_ctx, chunk, AAD = P || "context-chunk\0" ||
//   canonical({"index": i, "object": object_id}))`,
// - manifest (canonical JSON) with exactly `MANIFEST_KEYS`; `size` = sum of the sealed chunk lengths, `digest` = b64u
//   SHA-256 over the concatenated SHA-256 of every sealed chunk in index order,
// - `sig = Ed25519_author(P || "context\0" || canonical(manifest))`,
// - `tag = b64u(HMAC-SHA256(HKDF(WK_e, info=P || "pr-link"), canonical({"number": n, "repo": "<owner>/<name>"})))`
//   with the repository in lower case,
// - sealed link body (section 4.11a, ADR-084): `k_body = HKDF(WK_e, info=P || "pr-link-body")`, `sealed =
//   b64u(nonce(12) || AES-256-GCM(k_body, pad(canonical({"number": n, "repo": "<owner>/<name>"})), AAD))` with the
//   repository as GitHub spells it, zero-padded to `PR_LINK_BODY_BYTES`, `AAD = P || "pr-link-body\0" || wg_id ||
//   "\0" || epoch || "\0" || tag || "\0" || task_id || "\0" || origin`, `origin` = `device:<session_id>` or
//   `user:<linked_by>`. `openPrLink` refuses another length (`header_invalid`), a body that does not open
//   (`decrypt_failed`), a padding or plaintext not exactly in sealed form and a recomputed tag other than the row's
//   (`header_invalid`).
//
// Check order (as in every implementation): manifest keys (`header_invalid`), canonical JSON (`non_ascii`), field
// formats (`header_invalid`); signature (`signature_invalid`); chunk count, size and digest (`header_invalid`); a chunk
// above `CHUNK_CT_BYTES` (`size_exceeded`); decryption (`decrypt_failed`).
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, copy, fromUtf8, utf8 } from '../encoding/bytes';
import { canonical, parseJson } from '../encoding/json';
import * as aead from './aead';
import { fail } from './errors';
import * as F from './fields';
import { hkdfAesKey, hkdfHmacKey, KEY_BYTES, SALT_BYTES } from './kdf';
import { ed25519VerifyB64u } from './keys';
import { label } from './labels';
import { Secret } from './secret';
import { sha256, subtle } from './subtle';

export const VERSION = 1;
/** Info label of `k_ctx` and signature label of the manifest (without the prefix P). */
export const LABEL_CONTEXT = 'context\0';
export const LABEL_CONTEXT_CHUNK = 'context-chunk\0';
export const PR_LINK = 'pr-link';
/** Info label of `k_body` (without the prefix P). */
export const PR_LINK_BODY = 'pr-link-body';
export const LABEL_PR_LINK_BODY = 'pr-link-body\0';
/** The padded plaintext of a sealed link body. */
export const PR_LINK_BODY_BYTES = 256;
/** The decoded length of every `sealed` text. */
export const PR_LINK_SEALED_BYTES = aead.NONCE_BYTES + PR_LINK_BODY_BYTES + aead.TAG_BYTES;
const ORIGIN_KINDS = ['device:', 'user:'] as const;
/** At most this many compressed archive bytes per chunk. */
export const CHUNK_BYTES = 2 * 1024 * 1024;
/** The largest sealed chunk. */
export const CHUNK_CT_BYTES = CHUNK_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES;
/** The smallest sealed chunk (a chunk holds at least one byte). */
export const MIN_CT_BYTES = aead.NONCE_BYTES + aead.TAG_BYTES + 1;
export const MAX_CHUNKS = 1_000_000;
export const MAX_EPOCH = 2 ** 31 - 1;
const ID_KEYS = ['object', 'org', 'wg', 'task', 'session', 'author'] as const;
export const MANIFEST_KEYS = ['v', ...ID_KEYS, 'epoch', 'salt', 'chunks', 'size', 'digest', 'ts'] as const;
const REPO_RE = /^[a-z0-9_.-]{1,100}\/[a-z0-9_.-]{1,100}$/;

export type Manifest = Record<(typeof MANIFEST_KEYS)[number], unknown>;

function workgroupBytes(workgroupKey: Secret): Secret {
  if (workgroupKey.length !== KEY_BYTES) fail('header_invalid');
  return workgroupKey;
}

/** `k_ctx` as a non-extractable AES-256-GCM key; workgroup key and salt 32 bytes (`header_invalid`). */
export function contextKey(workgroupKey: Secret, salt: Uint8Array, objectId: string): Promise<CryptoKey> {
  const info = concat(label(LABEL_CONTEXT), utf8(F.uuid(objectId)));
  return hkdfAesKey(workgroupBytes(workgroupKey), info, F.requireBytes(salt, SALT_BYTES));
}

/** `P || "context-chunk\0" || canonical({"index": i, "object": object_id})`. */
export function chunkAad(objectId: string, index: number): Uint8Array<ArrayBuffer> {
  const position = F.requireInt(index, 0, MAX_CHUNKS - 1);
  return concat(label(LABEL_CONTEXT_CHUNK), canonical({ index: position, object: F.uuid(objectId) }));
}

/** `nonce || AES-GCM(k_ctx, chunk, AAD)`; an empty or too large chunk is `size_exceeded`. */
export function sealChunk(
  key: CryptoKey,
  objectId: string,
  index: number,
  chunk: Uint8Array,
  nonce?: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  if (chunk.length < 1 || chunk.length > CHUNK_BYTES) fail('size_exceeded');
  return aead.sealBlob(key, chunk, chunkAad(objectId, index), nonce);
}

/** The compressed bytes of one chunk (`size_exceeded` above `CHUNK_CT_BYTES`, `decrypt_failed` otherwise). */
export function openChunk(
  key: CryptoKey,
  objectId: string,
  index: number,
  sealed: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  if (sealed.length > CHUNK_CT_BYTES) fail('size_exceeded');
  return aead.openBlob(key, sealed, chunkAad(objectId, index));
}

/** The manifest `digest` of the sealed chunks in index order. */
export async function digest(chunks: readonly Uint8Array[]): Promise<string> {
  const hashes = await Promise.all(chunks.map((sealed) => sha256(sealed)));
  return b64uEncode(await sha256(concat(...hashes)));
}

/** Exact key set, canonical JSON and field formats; returns the manifest. */
export function checkManifest(value: unknown): Manifest {
  const manifest = F.headerDict(value, MANIFEST_KEYS) as Manifest;
  F.literal(manifest.v, VERSION);
  for (const key of ID_KEYS) F.uuid(manifest[key]);
  F.requireInt(manifest.epoch, 1, MAX_EPOCH);
  F.b64uBytes(manifest.salt, SALT_BYTES);
  const chunks = F.requireInt(manifest.chunks, 1, MAX_CHUNKS);
  F.requireInt(manifest.size, chunks * MIN_CT_BYTES, chunks * CHUNK_CT_BYTES);
  F.b64uBytes(manifest.digest, 32);
  F.time(manifest.ts);
  return manifest;
}

/** `P || "context\0" || canonical(manifest)`, the signed text. */
export function manifestText(manifest: Manifest): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_CONTEXT), canonical(manifest));
}

/** Format, then the author's signature; returns the checked manifest. */
export async function verifyManifest(manifest: unknown, signature: unknown, authorSigPublic: Uint8Array) {
  const checked = checkManifest(manifest);
  await ed25519VerifyB64u(authorSigPublic, signature, manifestText(checked));
  return checked;
}

/** The sealed chunks fit the manifest: each at most `CHUNK_CT_BYTES`, then count, size and digest. */
export async function checkChunks(manifest: Manifest, chunks: readonly Uint8Array[]): Promise<void> {
  if (chunks.some((sealed) => sealed.length > CHUNK_CT_BYTES)) fail('size_exceeded');
  const size = chunks.reduce((sum, sealed) => sum + sealed.length, 0);
  if (chunks.length !== manifest.chunks || size !== manifest.size || (await digest(chunks)) !== manifest.digest) {
    fail('header_invalid');
  }
}

/** Verify manifest and chunks, decrypt and join: the compressed archive. */
export async function openContext(
  workgroupKey: Secret,
  manifest: unknown,
  signature: unknown,
  authorSigPublic: Uint8Array,
  chunks: readonly Uint8Array[],
): Promise<Uint8Array<ArrayBuffer>> {
  const checked = await verifyManifest(manifest, signature, authorSigPublic);
  await checkChunks(checked, chunks);
  const objectId = checked.object as string;
  const key = await contextKey(workgroupKey, b64uDecode(checked.salt), objectId);
  const parts = [];
  for (const [index, sealed] of chunks.entries()) parts.push(await openChunk(key, objectId, index, sealed));
  return concat(...parts);
}

/** `HKDF(WK_e, info=P || "pr-link")` as a non-extractable HMAC-SHA256 key. */
export function prLinkKey(workgroupKey: Secret): Promise<CryptoKey> {
  return hkdfHmacKey(workgroupBytes(workgroupKey), label(PR_LINK));
}

/** `canonical({"number": n, "repo": repo})` with the repository `<owner>/<name>` in lower case. */
export function prLinkText(repo: unknown, number: unknown): Uint8Array<ArrayBuffer> {
  if (typeof repo !== 'string') fail('header_invalid');
  const lowered = repo.toLowerCase();
  const text = canonical({ number: F.requireInt(number, 1), repo: lowered });
  if (!REPO_RE.test(lowered)) fail('header_invalid');
  return text;
}

/** The b64u tag of a pull request (the hub never learns the repository or the number). */
export async function prLinkTag(linkKey: CryptoKey, repo: unknown, number: unknown): Promise<string> {
  return b64uEncode(new Uint8Array(await subtle.sign('HMAC', linkKey, copy(prLinkText(repo, number)))));
}

/** `k_body = HKDF(WK_e, info=P || "pr-link-body")` as a non-extractable AES-256-GCM key. */
export function prLinkBodyKey(workgroupKey: Secret): Promise<CryptoKey> {
  return hkdfAesKey(workgroupBytes(workgroupKey), label(PR_LINK_BODY));
}

/** What a sealed link body is bound to (the AAD); `origin` is `device:<session_id>` or `user:<linked_by>`. */
export interface PrLinkBinding {
  wgId: string;
  epoch: number;
  tag: string;
  taskId: string;
  origin: string;
}

/** `device:<session_id>` or `user:<linked_by>` with a UUIDv7, else `header_invalid`. */
export function prLinkOrigin(origin: unknown): string {
  if (typeof origin !== 'string') fail('header_invalid');
  const kind = ORIGIN_KINDS.find((prefix) => origin.startsWith(prefix));
  if (kind === undefined) fail('header_invalid');
  F.uuid(origin.slice(kind.length));
  return origin;
}

/** `P || "pr-link-body\0" || wg_id || "\0" || epoch || "\0" || tag || "\0" || task_id || "\0" || origin`. */
export function prLinkBodyAad(binding: PrLinkBinding): Uint8Array<ArrayBuffer> {
  const parts = [
    F.uuid(binding.wgId),
    String(F.requireInt(binding.epoch, 1, MAX_EPOCH)),
    F.b64uBytes(binding.tag, 32),
    F.uuid(binding.taskId),
    prLinkOrigin(binding.origin),
  ];
  return concat(label(LABEL_PR_LINK_BODY), utf8(parts.join('\0')));
}

/** `canonical({"number": n, "repo": repo})` with the repository as given (checked like `prLinkText`). */
export function prLinkBodyText(repo: unknown, number: unknown): Uint8Array<ArrayBuffer> {
  prLinkText(repo, number);
  return canonical({ number, repo });
}

/** The b64u `sealed` body of a link; `nonce` is fixed only by the test vectors. */
export async function sealPrLink(
  bodyKey: CryptoKey,
  link: PrLinkBinding & { repo: string; number: number },
  nonce?: Uint8Array,
): Promise<string> {
  const aad = prLinkBodyAad(link);
  const text = prLinkBodyText(link.repo, link.number);
  const padded = concat(text, new Uint8Array(PR_LINK_BODY_BYTES - text.length));
  return b64uEncode(await aead.sealBlob(bodyKey, padded, aad, nonce));
}

/** `{repo, number}` of a sealed body whose recomputed tag is `binding.tag`; refusals as in the header comment. */
export async function openPrLink(
  bodyKey: CryptoKey,
  linkKey: CryptoKey,
  binding: PrLinkBinding,
  sealed: unknown,
): Promise<{ repo: string; number: number }> {
  const aad = prLinkBodyAad(binding);
  const blob = b64uDecode(sealed, PR_LINK_SEALED_BYTES);
  const padded = await aead.openBlob(bodyKey, blob, aad);
  let end = padded.length;
  while (end > 0 && padded[end - 1] === 0) end -= 1;
  const text = padded.subarray(0, end);
  const body = F.requireKeys(parseJson(text), ['number', 'repo']);
  const { repo, number } = body;
  if (
    fromUtf8(prLinkBodyText(repo, number)) !== fromUtf8(text) ||
    (await prLinkTag(linkKey, repo, number)) !== binding.tag
  ) {
    fail('header_invalid');
  }
  return { repo: repo as string, number: number as number };
}
