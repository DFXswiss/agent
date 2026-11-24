// Reading the encrypted log of a test run or a work VM job (ADR-080 section 3, 03-api.md section 4.6,
// 04-crypto.md section 4.12):
//
// - `openLog`: `GET /v1/tasks/{task}/logs/{object}` (manifest, signature). The manifest must have the exact format
//   (`checkManifest`) and name what the encrypted event (or operation result) that pointed to the log says: `object`
//   its `log.object_id`, `ref` its `run_id` / `job_id`, its task and workgroup, `bytes` and `stored` its
//   `log.bytes` / `log.stored_bytes`. The signature is checked with the signing key of the `author` device from the
//   verified membership log of the manifest's org (the same keys the event reader checks event authors with); then
//   `k_log` is derived from the workgroup key of the manifest's `epoch` (`header_invalid`, `author_not_writer`,
//   `signature_invalid`, `keys_pending` when the user holds no key of that epoch);
// - `readChunk`: `GET .../chunks/{index}`, opened with `k_log` (the AAD binds object and index); every chunk but the
//   last holds exactly `CHUNK_BYTES`, the last the rest of `stored`, so a page is found by offset;
// - `readAll`: every chunk in order (the download), then size and `digest` of the sealed chunks against the manifest;
// - `LogPages`: the text of one page (one chunk, `text.ts`) with a small cache of opened chunks;
// - `locateEvent`: the workgroup and task of the verified event at `seq` whose body is `body` (where a chat item's
//   log lives).
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat } from '../encoding/bytes';
import { fail } from '../crypto/errors';
import { CHUNK_BYTES, checkManifest, logKey, type Manifest, openChunk, verifyManifest } from '../crypto/runlog';
import { sha256 } from '../crypto/subtle';
import type { LogRef } from '../chat/shape';
import { DEVICE, epochKey, WG } from '../log/state';
import { RequestError } from '../requests/client';
import { isRecord } from '../registry';
import { orgEntry } from '../store/slices/orgs';
import type { StoreState } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import { decodeWindow, missingBytes } from './text';

/** Where a log is named: the event's workgroup and task, the run or job ID and the log reference. */
export interface LogSource {
  readonly wg: string;
  readonly task: string;
  /** `run_id` of a test run or `job_id` of a work VM job. */
  readonly ref: string;
  readonly log: LogRef;
}

export interface OpenedLog {
  readonly source: LogSource;
  readonly manifest: Manifest;
  readonly key: CryptoKey;
  readonly chunks: number;
  /** The output bytes kept. */
  readonly stored: number;
  /** The full output length. */
  readonly bytes: number;
  /** Only the last `stored` of `bytes` are kept. */
  readonly truncated: boolean;
}

const ids = (source: LogSource) => ({
  task: encodeURIComponent(source.task),
  object: encodeURIComponent(source.log.object_id),
});

/** The checked manifest and the chunk key of the log of `source` (module comment). */
export async function openLog(context: TasksContext, source: LogSource): Promise<OpenedLog> {
  const { task, object } = ids(source);
  const body = await context.client.request('GET', `/v1/tasks/${task}/logs/${object}`);
  const manifest = checkManifest(isRecord(body) ? body.manifest : null);
  if (
    manifest.object !== source.log.object_id ||
    manifest.ref !== source.ref ||
    manifest.task !== source.task ||
    manifest.wg !== source.wg ||
    manifest.bytes !== source.log.bytes ||
    manifest.stored !== source.log.stored_bytes
  ) {
    fail('header_invalid');
  }
  const found = orgEntry(context.store.getState().orgs, manifest.org as string);
  const author = found.log?.state.principals.get(manifest.author as string);
  if (author?.kind !== DEVICE) fail('author_not_writer');
  await verifyManifest(manifest, (body as Record<string, unknown>).sig, b64uDecode(author.sig));
  const workgroupKey = found.keys.get(epochKey(WG, source.wg, manifest.epoch as number));
  if (workgroupKey === undefined) throw new RequestError('keys_pending');
  const key = await logKey(workgroupKey, b64uDecode(manifest.salt), source.log.object_id);
  const stored = manifest.stored as number;
  const bytes = manifest.bytes as number;
  return { source, manifest, key, chunks: manifest.chunks as number, stored, bytes, truncated: stored < bytes };
}

async function fetchSealed(context: TasksContext, log: OpenedLog, index: number): Promise<Uint8Array> {
  const { task, object } = ids(log.source);
  const body = await context.client.request('GET', `/v1/tasks/${task}/logs/${object}/chunks/${index}`);
  return b64uDecode(isRecord(body) ? body.ct : null);
}

/** The output bytes of chunk `index`: opened, and exactly as long as fixed-size chunks of `stored` require. */
async function openPiece(log: OpenedLog, index: number, sealed: Uint8Array): Promise<Uint8Array> {
  const piece = await openChunk(log.key, log.source.log.object_id, index, sealed);
  const length = index < log.chunks - 1 ? CHUNK_BYTES : log.stored - (log.chunks - 1) * CHUNK_BYTES;
  if (piece.length !== length) fail('header_invalid');
  return piece;
}

/** The output bytes of chunk `index`. */
export async function readChunk(context: TasksContext, log: OpenedLog, index: number): Promise<Uint8Array> {
  return openPiece(log, index, await fetchSealed(context, log, index));
}

/** All output bytes kept, one part per chunk, after the size and digest check of the sealed chunks. */
export async function readAll(context: TasksContext, log: OpenedLog): Promise<Uint8Array[]> {
  const parts: Uint8Array[] = [];
  const hashes: Uint8Array[] = [];
  let size = 0;
  for (let index = 0; index < log.chunks; index += 1) {
    const sealed = await fetchSealed(context, log, index);
    size += sealed.length;
    hashes.push(await sha256(sealed));
    parts.push(await openPiece(log, index, sealed));
  }
  if (size !== log.manifest.size || b64uEncode(await sha256(concat(...hashes))) !== log.manifest.digest) {
    fail('header_invalid');
  }
  return parts;
}

/** Opened chunks kept for paging. */
export const CACHED_CHUNKS = 4;

/** The pages of an opened log: one chunk each, as text (`text.ts`); the last `CACHED_CHUNKS` chunks are kept. */
export class LogPages {
  readonly #cache = new Map<number, Promise<Uint8Array>>();

  constructor(
    readonly context: TasksContext,
    readonly log: OpenedLog,
  ) {}

  #chunk(index: number): Promise<Uint8Array> {
    const known = this.#cache.get(index);
    if (known !== undefined) return known;
    const read = readChunk(this.context, this.log, index);
    // a failed read is tried again on the next visit
    read.catch(() => this.#cache.delete(index));
    this.#cache.set(index, read);
    while (this.#cache.size > CACHED_CHUNKS) this.#cache.delete(this.#cache.keys().next().value!);
    return read;
  }

  /** The text of page `index` (0 ≤ index < chunks). */
  async page(index: number): Promise<string> {
    const bytes = await this.#chunk(index);
    const next = missingBytes(bytes) > 0 && index < this.log.chunks - 1 ? await this.#chunk(index + 1) : null;
    return decodeWindow(bytes, index > 0 || this.log.truncated, next);
  }
}

/** The workgroup and task of the verified event at `seq` whose decrypted body is `body`, else null. */
export function locateEvent(
  state: StoreState,
  seq: number,
  body: unknown,
): { readonly wg: string; readonly task: string } | null {
  for (const [wg, scope] of Object.entries(state.events)) {
    const entry = scope.entries.get(seq);
    if (entry?.status === 'verified' && entry.body === body) return { wg, task: entry.header!.task as string };
  }
  return null;
}
