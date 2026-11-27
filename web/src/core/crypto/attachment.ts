// Files and images attached to a prompt (04-crypto.md section 4.14, ADR-102), twin of `agent_hub.crypto.attachment`
// (vectors `contracts/vectors/attachment.json`). The browser seals the file before it uploads it and opens it when the
// chat shows it; the hub never sees name, type or content:
//
// - `k_att = HKDF(WK_e, salt=ref.salt (32 B), info=P || "attachment\0" || object_id)`,
// - chunk `i` = `nonce(12) || AES-256-GCM(k_att, piece, AAD = P || "attachment-chunk\0" ||
//   canonical({"index": i, "object": object_id}))`, pieces of at most `CHUNK_BYTES` file bytes,
// - no manifest of its own: the prompt body names the file with a reference of exactly `REF_KEYS`, signed with the
//   prompt; `digest` is the b64u SHA-256 over the SHA-256 of every sealed chunk in index order.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { canonical } from '../encoding/json';
import * as aead from './aead';
import { fail } from './errors';
import * as F from './fields';
import { hkdfAesKey, KEY_BYTES, SALT_BYTES } from './kdf';
import { label } from './labels';
import type { Secret } from './secret';
import { random, sha256 } from './subtle';

/** Info label of `k_att` (without the prefix P). */
export const LABEL_ATTACHMENT = 'attachment\0';
export const LABEL_ATTACHMENT_CHUNK = 'attachment-chunk\0';
/** At most this many file bytes per chunk. */
export const CHUNK_BYTES = 1024 * 1024;
export const CHUNK_CT_BYTES = CHUNK_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES;
export const MIN_CT_BYTES = aead.NONCE_BYTES + aead.TAG_BYTES + 1;
/** The largest file. */
export const MAX_BYTES = 32 * CHUNK_BYTES;
export const MAX_CHUNKS = MAX_BYTES / CHUNK_BYTES;
/** At most this many attachments per prompt. */
export const MAX_ATTACHMENTS = 10;
export const MAX_EPOCH = 2 ** 31 - 1;
export const NAME_MAX = 255;
export const TYPE_MAX = 127;
export const REF_KEYS = ['object', 'name', 'type', 'bytes', 'epoch', 'salt', 'digest'] as const;
/** A media type in lower case without parameters. */
export const TYPE_RE = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/;
// eslint-disable-next-line no-control-regex -- control characters are exactly what a file name must not hold
const NAME_FORBIDDEN_RE = /[\u0000-\u001f\u007f/\\]/;

/** One attachment as a prompt body names it. */
export interface AttachmentRef {
  readonly object: string;
  readonly name: string;
  readonly type: string;
  readonly bytes: number;
  readonly epoch: number;
  readonly salt: string;
  readonly digest: string;
}

/** `k_att` as a non-extractable AES-256-GCM key; workgroup key and salt 32 bytes (`header_invalid`). */
export function attachmentKey(workgroupKey: Secret, salt: Uint8Array, objectId: string): Promise<CryptoKey> {
  if (workgroupKey.length !== KEY_BYTES) fail('header_invalid');
  const info = concat(label(LABEL_ATTACHMENT), utf8(F.uuid(objectId)));
  return hkdfAesKey(workgroupKey, info, F.requireBytes(salt, SALT_BYTES));
}

/** `P || "attachment-chunk\0" || canonical({"index": i, "object": object_id})`. */
export function chunkAad(objectId: string, index: number): Uint8Array<ArrayBuffer> {
  const position = F.requireInt(index, 0, MAX_CHUNKS - 1);
  return concat(label(LABEL_ATTACHMENT_CHUNK), canonical({ index: position, object: F.uuid(objectId) }));
}

/** `nonce || AES-GCM(k_att, piece, AAD)`; an empty or too large piece is `size_exceeded`. */
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

/** The file bytes of one chunk (`size_exceeded` above `CHUNK_CT_BYTES`, `decrypt_failed` otherwise). */
export function openChunk(
  key: CryptoKey,
  objectId: string,
  index: number,
  sealed: Uint8Array,
): Promise<Uint8Array<ArrayBuffer>> {
  if (sealed.length > CHUNK_CT_BYTES) fail('size_exceeded');
  return aead.openBlob(key, sealed, chunkAad(objectId, index));
}

/** The number of chunks of a file of `size` bytes. */
export function chunkCount(size: number): number {
  return Math.ceil(size / CHUNK_BYTES);
}

/** The sealed bytes of all chunks of a file of `size` bytes. */
export function sealedSize(size: number): number {
  return size + chunkCount(size) * (aead.NONCE_BYTES + aead.TAG_BYTES);
}

/** The `digest` of the sealed chunks in index order. */
export async function digest(chunks: readonly Uint8Array[]): Promise<string> {
  const hashes = await Promise.all(chunks.map((sealed) => sha256(sealed)));
  return b64uEncode(await sha256(concat(...hashes)));
}

/** A file name: 1 to `NAME_MAX` characters (code points), no control character, no `/` or `\`, not `.` or `..`. */
export function checkName(value: unknown): string {
  if (
    typeof value !== 'string' ||
    [...value].length < 1 ||
    [...value].length > NAME_MAX ||
    NAME_FORBIDDEN_RE.test(value) ||
    value === '.' ||
    value === '..'
  ) {
    fail('header_invalid');
  }
  return value;
}

/** A media type of `TYPE_RE`, at most `TYPE_MAX` characters. */
export function checkType(value: unknown): string {
  if (typeof value !== 'string' || value.length > TYPE_MAX || !TYPE_RE.test(value)) fail('header_invalid');
  return value;
}

/** Exact key set and field formats of one reference of a prompt body. */
export function checkRef(value: unknown): AttachmentRef {
  const ref = F.requireKeys(value, REF_KEYS);
  F.uuid(ref.object);
  checkName(ref.name);
  checkType(ref.type);
  F.requireInt(ref.bytes, 1, MAX_BYTES);
  F.requireInt(ref.epoch, 1, MAX_EPOCH);
  F.b64uBytes(ref.salt, SALT_BYTES);
  F.b64uBytes(ref.digest, 32);
  return ref as unknown as AttachmentRef;
}

/** The `attachments` of a prompt body: at most `MAX_ATTACHMENTS` references with distinct objects. */
export function checkRefs(value: unknown): AttachmentRef[] {
  if (!Array.isArray(value) || value.length > MAX_ATTACHMENTS) fail('header_invalid');
  const refs = value.map(checkRef);
  if (new Set(refs.map((ref) => ref.object)).size !== refs.length) fail('header_invalid');
  return refs;
}

/** The sealed chunks fit the reference: each at most `CHUNK_CT_BYTES`, then count, size and digest. */
export async function checkChunks(ref: AttachmentRef, chunks: readonly Uint8Array[]): Promise<void> {
  if (chunks.some((sealed) => sealed.length > CHUNK_CT_BYTES)) fail('size_exceeded');
  const size = chunks.reduce((sum, sealed) => sum + sealed.length, 0);
  if (
    chunks.length !== chunkCount(ref.bytes) ||
    size !== sealedSize(ref.bytes) ||
    (await digest(chunks)) !== ref.digest
  ) {
    fail('header_invalid');
  }
}

/** The file in pieces of at most `CHUNK_BYTES` (an empty or too large file is `size_exceeded`). */
export function split(data: Uint8Array): Uint8Array[] {
  if (data.length < 1 || data.length > MAX_BYTES) fail('size_exceeded');
  const parts = [];
  for (let start = 0; start < data.length; start += CHUNK_BYTES) parts.push(data.subarray(start, start + CHUNK_BYTES));
  return parts;
}

/** Seals a file under `WK` of `epoch`: the reference and the sealed chunks. Salt and nonces are random unless the
 *  test vectors fix them. */
export async function sealAttachment(
  workgroupKey: Secret,
  input: {
    readonly objectId: string;
    readonly name: string;
    readonly type: string;
    readonly epoch: number;
    readonly data: Uint8Array;
    readonly salt?: Uint8Array;
    readonly nonces?: readonly Uint8Array[];
  },
): Promise<{ readonly ref: AttachmentRef; readonly chunks: Uint8Array<ArrayBuffer>[] }> {
  const parts = split(input.data);
  if (input.nonces !== undefined && input.nonces.length !== parts.length) fail('header_invalid');
  const salt = input.salt ?? random(SALT_BYTES);
  const key = await attachmentKey(workgroupKey, salt, input.objectId);
  const chunks = [];
  for (const [index, part] of parts.entries()) {
    chunks.push(await sealChunk(key, input.objectId, index, part, input.nonces?.[index]));
  }
  const ref = checkRef({
    object: input.objectId,
    name: input.name,
    type: input.type,
    bytes: input.data.length,
    epoch: input.epoch,
    salt: b64uEncode(salt),
    digest: await digest(chunks),
  });
  return { ref, chunks };
}

/** Checks the reference and the chunks, decrypts and joins: the file. `workgroupKey` is `WK` of `ref.epoch`. */
export async function openAttachment(
  workgroupKey: Secret,
  ref: unknown,
  chunks: readonly Uint8Array[],
): Promise<Uint8Array<ArrayBuffer>> {
  const checked = checkRef(ref);
  await checkChunks(checked, chunks);
  const key = await attachmentKey(workgroupKey, b64uDecode(checked.salt), checked.object);
  const parts = [];
  for (const [index, sealed] of chunks.entries()) parts.push(await openChunk(key, checked.object, index, sealed));
  return concat(...parts);
}
