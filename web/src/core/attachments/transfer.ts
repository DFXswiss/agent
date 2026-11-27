// Files and images attached to a prompt (ADR-102; 03-api.md sections 4.6 and 10; 04-crypto.md section 4.14).
//
// - `pendingFile`: a file the person chose, dropped or pasted, with a name and media type the prompt body accepts
//   (`fileName`, `mediaType`), checked against the limits (`checkFiles`: at most 10 per prompt, 1 byte to 32 MiB
//   each);
// - `uploadAttachment`: seals the file under the workgroup's current key, uploads every chunk
//   (`PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}`) and commits the object (`POST …/commit`); the
//   answer is the reference the prompt body names. A prompt is sealed only after all its files are committed, so it
//   never names an object the hub does not hold complete. An upload of the same pending file again (a retry) uses the
//   same object and bytes: the hub answers `replayed`;
// - `downloadAttachment`: the chunks of a complete object (`GET …/chunks/{index}`), checked against the verified
//   reference and opened with `WK` of the reference's epoch.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { uuid7 } from '../encoding/ids';
import {
  type AttachmentRef,
  checkType,
  chunkCount,
  MAX_ATTACHMENTS,
  MAX_BYTES,
  NAME_MAX,
  openAttachment,
  sealAttachment,
} from '../crypto/attachment';
import type { Secret } from '../crypto/secret';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { epochKey, WG } from '../log/state';
import { orgEntry } from '../store/slices/orgs';
import type { TasksContext } from '../tasks/pull';
import { SendError, workgroupKey } from '../tasks/send';

export type { AttachmentRef } from '../crypto/attachment';

/** Media types shown as a picture in the chat (others are offered as a download). */
export const IMAGE_TYPES: ReadonlySet<string> = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
/** The media type of a file the browser names none (or none the prompt body accepts) for. */
export const UNKNOWN_TYPE = 'application/octet-stream';
// eslint-disable-next-line no-control-regex -- control characters are exactly what a file name must not hold
const NAME_UNSAFE_RE = /[\u0000-\u001f\u007f/\\]/g;

/** A file waiting to be attached: its bytes with the name and media type the reference will carry. */
export interface PendingFile {
  readonly id: string;
  readonly name: string;
  readonly type: string;
  readonly data: Uint8Array;
}

/** A file name the prompt body accepts: control characters, `/` and `\` replaced by `_`, at most `NAME_MAX` code
 *  points (the end kept, so the extension survives), `file` for an empty name, `.` or `..`. */
export function fileName(raw: string): string {
  const points = [...raw.replace(NAME_UNSAFE_RE, '_')];
  const cut = points.length > NAME_MAX ? points.slice(points.length - NAME_MAX) : points;
  const name = cut.join('');
  return name === '' || name === '.' || name === '..' ? 'file' : name;
}

/** The media type of a file: the browser's type in lower case without parameters, `UNKNOWN_TYPE` when there is none
 *  or the prompt body would not accept it. */
export function mediaType(raw: string): string {
  const type = raw.split(';')[0]!.trim().toLowerCase();
  try {
    return checkType(type);
  } catch {
    return UNKNOWN_TYPE;
  }
}

/** A chosen, dropped or pasted file ready to attach (`id` tells two files of the same name apart). */
export function pendingFile(id: string, name: string, type: string, data: Uint8Array): PendingFile {
  return { id, name: fileName(name), type: mediaType(type), data };
}

/** Why files cannot be attached, or null: more than `MAX_ATTACHMENTS` (`too_many`), an empty file (`empty`) or one
 *  above `MAX_BYTES` (`too_large`). These are checks of the composer, not codes of the hub. */
export type FilesProblem = 'too_many' | 'empty' | 'too_large';

export function checkFiles(files: readonly { readonly size: number }[]): FilesProblem | null {
  if (files.length > MAX_ATTACHMENTS) return 'too_many';
  if (files.some((file) => file.size < 1)) return 'empty';
  if (files.some((file) => file.size > MAX_BYTES)) return 'too_large';
  return null;
}

/** Whether the chat shows the attachment as a picture. */
export function isImage(ref: AttachmentRef): boolean {
  return IMAGE_TYPES.has(ref.type);
}

function path(wg: string, object: string, tail: string): string {
  return `/v1/workgroups/${encodeURIComponent(wg)}/attachments/${encodeURIComponent(object)}/${tail}`;
}

/** Seals, uploads and commits one file of workgroup `wg`; the reference for the prompt body. `objectId` keeps the
 *  object of an earlier attempt (a retry replays it). */
export async function uploadAttachment(
  context: TasksContext,
  org: string,
  wg: string,
  file: PendingFile,
  objectId: string = uuid7(context.now()),
): Promise<AttachmentRef> {
  const [epoch, key] = await workgroupKey(context, org, wg);
  const { ref, chunks } = await sealAttachment(key, {
    objectId,
    name: file.name,
    type: file.type,
    epoch,
    data: file.data,
  });
  for (const [index, chunk] of chunks.entries()) {
    await context.client.request('PUT', path(wg, objectId, `chunks/${index}`), { body: { ct: b64uEncode(chunk) } });
  }
  const size = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  await context.client.request('POST', path(wg, objectId, 'commit'), {
    body: { chunks: chunks.length, size, digest: ref.digest },
  });
  return ref;
}

/** `WK` of `epoch` of workgroup `wg`; asks the log and wrap jobs once if it is not in memory (`epoch_stale`). */
async function keyOf(context: TasksContext, org: string, wg: string, epoch: number): Promise<Secret> {
  const lookup = () => orgEntry(context.store.getState().orgs, org).keys.get(epochKey(WG, wg, epoch)) ?? null;
  const known = lookup();
  if (known !== null) return known;
  await context.refreshLog(org);
  const found = lookup();
  if (found === null) throw new SendError('epoch_stale');
  return found;
}

/** The opened file of a verified reference of a prompt of workgroup `wg`. */
export async function downloadAttachment(
  context: TasksContext,
  org: string,
  wg: string,
  ref: AttachmentRef,
): Promise<Uint8Array<ArrayBuffer>> {
  const key = await keyOf(context, org, wg, ref.epoch);
  const chunks = [];
  for (let index = 0; index < chunkCount(ref.bytes); index += 1) {
    const answer = await context.client.request('GET', path(wg, ref.object, `chunks/${index}`));
    if (!isRecord(answer) || typeof answer.ct !== 'string') throw new HubError('unavailable', 200);
    chunks.push(b64uDecode(answer.ct));
  }
  return openAttachment(key, ref, chunks);
}
