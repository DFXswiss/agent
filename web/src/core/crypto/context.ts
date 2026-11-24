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
//   with the repository in lower case.
//
// Check order (as in every implementation): manifest keys (`header_invalid`), canonical JSON (`non_ascii`), field
// formats (`header_invalid`); signature (`signature_invalid`); chunk count, size and digest (`header_invalid`); a chunk
// above `CHUNK_CT_BYTES` (`size_exceeded`); decryption (`decrypt_failed`).
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, copy, utf8 } from '../encoding/bytes';
import { canonical } from '../encoding/json';
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
