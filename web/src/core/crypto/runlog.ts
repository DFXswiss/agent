// Logs of test runs and work VM jobs (04-crypto.md section 4.12, ADR-079 section 3), twin of `agent_hub.crypto.runlog`
// (vectors `contracts/vectors/runlog.json`). The browser holds the workgroup key, so it opens the chunks of a log
// (`GET /v1/tasks/{task}/logs/{object}/chunks/{index}`) itself; the hub never sees the output:
//
// - `k_log = HKDF(WK_e, salt=manifest.salt (32 B), info=P || "run-log\0" || object_id)`,
// - chunk `i` = `nonce(12) || AES-256-GCM(k_log, piece, AAD = P || "run-log-chunk\0" ||
//   canonical({"index": i, "object": object_id}))`, pieces of at most `CHUNK_BYTES` uncompressed output bytes,
// - manifest (canonical JSON) with exactly `MANIFEST_KEYS` (`kind` = "log", `ref` = the run or job ID, `bytes` = the
//   full output length, `stored` = the bytes kept),
// - `sig = Ed25519_author(P || "run-log\0" || canonical(manifest))`.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { canonical } from '../encoding/json';
import * as aead from './aead';
import { fail } from './errors';
import * as F from './fields';
import { hkdfAesKey, KEY_BYTES, SALT_BYTES } from './kdf';
import { ed25519VerifyB64u } from './keys';
import { label } from './labels';
import { Secret } from './secret';
import { sha256 } from './subtle';

export const VERSION = 1;
export const KIND = 'log';
/** Info label of `k_log` and signature label of the manifest (without the prefix P). */
export const LABEL_RUN_LOG = 'run-log\0';
export const LABEL_RUN_LOG_CHUNK = 'run-log-chunk\0';
/** At most this many output bytes per chunk. */
export const CHUNK_BYTES = 256 * 1024;
export const CHUNK_CT_BYTES = CHUNK_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES;
export const MIN_CT_BYTES = aead.NONCE_BYTES + aead.TAG_BYTES + 1;
export const MAX_CHUNKS = 1_000_000;
export const MAX_EPOCH = 2 ** 31 - 1;
const ID_KEYS = ['object', 'org', 'wg', 'task', 'session', 'author'] as const;
export const MANIFEST_KEYS = [
  'v',
  'kind',
  'ref',
  ...ID_KEYS,
  'epoch',
  'salt',
  'chunks',
  'size',
  'digest',
  'bytes',
  'stored',
  'ts',
] as const;
const REF_RE = /^(?:tr|j)-[0-9a-f]{12}$/;

export type Manifest = Record<(typeof MANIFEST_KEYS)[number], unknown>;

/** `k_log` as a non-extractable AES-256-GCM key; workgroup key and salt 32 bytes (`header_invalid`). */
export function logKey(workgroupKey: Secret, salt: Uint8Array, objectId: string): Promise<CryptoKey> {
  if (workgroupKey.length !== KEY_BYTES) fail('header_invalid');
  const info = concat(label(LABEL_RUN_LOG), utf8(F.uuid(objectId)));
  return hkdfAesKey(workgroupKey, info, F.requireBytes(salt, SALT_BYTES));
}

/** `P || "run-log-chunk\0" || canonical({"index": i, "object": object_id})`. */
export function chunkAad(objectId: string, index: number): Uint8Array<ArrayBuffer> {
  const position = F.requireInt(index, 0, MAX_CHUNKS - 1);
  return concat(label(LABEL_RUN_LOG_CHUNK), canonical({ index: position, object: F.uuid(objectId) }));
}

/** `nonce || AES-GCM(k_log, piece, AAD)`; an empty or too large piece is `size_exceeded`. */
export function sealChunk(
  key: CryptoKey,
  objectId: string,
  index: number,
  piece: Uint8Array,
  nonce?: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  if (piece.length < 1 || piece.length > CHUNK_BYTES) fail('size_exceeded');
  return aead.sealBlob(key, piece, chunkAad(objectId, index), nonce);
}

/** The output bytes of one chunk (`size_exceeded` above `CHUNK_CT_BYTES`, `decrypt_failed` otherwise). */
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
  F.literal(manifest.kind, KIND);
  if (typeof manifest.ref !== 'string' || !REF_RE.test(manifest.ref)) fail('header_invalid');
  for (const key of ID_KEYS) F.uuid(manifest[key]);
  F.requireInt(manifest.epoch, 1, MAX_EPOCH);
  F.b64uBytes(manifest.salt, SALT_BYTES);
  const chunks = F.requireInt(manifest.chunks, 1, MAX_CHUNKS);
  F.requireInt(manifest.size, chunks * MIN_CT_BYTES, chunks * CHUNK_CT_BYTES);
  F.b64uBytes(manifest.digest, 32);
  const stored = F.requireInt(manifest.stored, (chunks - 1) * CHUNK_BYTES + 1, chunks * CHUNK_BYTES);
  F.requireInt(manifest.bytes, stored);
  F.time(manifest.ts);
  return manifest;
}

/** `P || "run-log\0" || canonical(manifest)`, the signed text. */
export function manifestText(manifest: Manifest): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_RUN_LOG), canonical(manifest));
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

/** Verify manifest and chunks, decrypt and join: the kept output. */
export async function openLog(
  workgroupKey: Secret,
  manifest: unknown,
  signature: unknown,
  authorSigPublic: Uint8Array,
  chunks: readonly Uint8Array[],
): Promise<Uint8Array<ArrayBuffer>> {
  const checked = await verifyManifest(manifest, signature, authorSigPublic);
  await checkChunks(checked, chunks);
  const objectId = checked.object as string;
  const key = await logKey(workgroupKey, b64uDecode(checked.salt), objectId);
  const parts = [];
  for (const [index, sealed] of chunks.entries()) parts.push(await openChunk(key, objectId, index, sealed));
  return concat(...parts);
}
