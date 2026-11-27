// Attachments of prompts (ADR-102) against the scriptable client of the task world: names and media types the prompt
// body accepts, the limits of the composer, the upload of a sealed file chunk by chunk with its commit, and the
// download of a complete object with the key of the reference's epoch.
import { describe, expect, it } from 'vitest';
import {
  checkFiles,
  downloadAttachment,
  fileName,
  isImage,
  mediaType,
  pendingFile,
  UNKNOWN_TYPE,
  uploadAttachment,
} from '../../../src/core/attachments/transfer';
import { CHUNK_BYTES, MAX_ATTACHMENTS, MAX_BYTES, openAttachment } from '../../../src/core/crypto/attachment';
import { Secret } from '../../../src/core/crypto/secret';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { epochKey, WG } from '../../../src/core/log/state';
import { addKeys, orgEntry } from '../../../src/core/store/slices/orgs';
import { type TaskWorld, taskWorld } from '../tasks/world';

const OBJECT = '0190f000-0000-7000-8000-00000000f001';

function hub(t: TaskWorld) {
  const chunks = new Map<string, string>();
  t.client.on(`PUT /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`, (body) => {
    chunks.set('0', (body as { ct: string }).ct);
    return { replayed: false };
  });
  t.client.on(`PUT /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/1`, (body) => {
    chunks.set('1', (body as { ct: string }).ct);
    return { replayed: false };
  });
  t.client.on(`POST /v1/workgroups/${t.wg}/attachments/${OBJECT}/commit`, () => ({
    completed_at: '2026-11-27T12:00:00.000Z',
    replayed: false,
  }));
  for (const index of ['0', '1']) {
    t.client.on(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/${index}`, () => ({ ct: chunks.get(index) }));
  }
  return chunks;
}

describe('pending files', () => {
  it('makes names the prompt body accepts', () => {
    expect(fileName('a/b\\c\u0000d.png')).toBe('a_b_c_d.png');
    expect(fileName(`${'x'.repeat(300)}.png`)).toBe(`${'x'.repeat(251)}.png`);
    expect(fileName('')).toBe('file');
    expect(fileName('.')).toBe('file');
    expect(fileName('..')).toBe('file');
    expect(fileName('Bericht 2026.pdf')).toBe('Bericht 2026.pdf');
  });

  it('takes the media type in lower case without parameters, else the unknown type', () => {
    expect(mediaType('Image/PNG; foo=bar')).toBe('image/png');
    expect(mediaType('')).toBe(UNKNOWN_TYPE);
    expect(mediaType('not a type')).toBe(UNKNOWN_TYPE);
    expect(pendingFile('id', 'a/b.txt', 'TEXT/plain', new Uint8Array([1]))).toEqual({
      id: 'id',
      name: 'a_b.txt',
      type: 'text/plain',
      data: new Uint8Array([1]),
    });
  });

  it('checks the limits of the composer', () => {
    expect(checkFiles([{ size: 1 }])).toBeNull();
    expect(checkFiles(Array.from({ length: MAX_ATTACHMENTS + 1 }, () => ({ size: 1 })))).toBe('too_many');
    expect(checkFiles([{ size: 0 }])).toBe('empty');
    expect(checkFiles([{ size: MAX_BYTES + 1 }])).toBe('too_large');
  });

  it('tells pictures from other files', () => {
    const ref = { object: OBJECT, name: 'a', bytes: 1, epoch: 1, salt: '', digest: '' };
    expect(isImage({ ...ref, type: 'image/png' })).toBe(true);
    expect(isImage({ ...ref, type: 'image/svg+xml' })).toBe(false);
    expect(isImage({ ...ref, type: 'application/pdf' })).toBe(false);
  });
});

describe('upload and download', () => {
  it('seals, uploads and commits a file of two chunks and opens it again', async () => {
    const t = await taskWorld();
    const chunks = hub(t);
    const data = new Uint8Array(CHUNK_BYTES + 3).map((_, index) => index % 7);
    const file = pendingFile('f1', 'log.txt', 'text/plain', data);
    const ref = await uploadAttachment(t.context, t.org, t.wg, file, OBJECT);
    expect(ref).toMatchObject({ object: OBJECT, name: 'log.txt', type: 'text/plain', bytes: data.length, epoch: 1 });
    expect(chunks.size).toBe(2);
    const sizes = [...chunks.values()].map((ct) => b64uDecode(ct).length);
    expect(t.client.of(`POST /v1/workgroups/${t.wg}/attachments/${OBJECT}/commit`)[0]!.body).toEqual({
      chunks: 2,
      size: sizes[0]! + sizes[1]!,
      digest: ref.digest,
    });
    const key = orgEntry(t.store.getState().orgs, t.org).keys.get(epochKey(WG, t.wg, 1))!;
    expect(
      await openAttachment(
        key,
        ref,
        [...chunks.values()].map((ct) => b64uDecode(ct)),
      ),
    ).toEqual(data);
    expect(await downloadAttachment(t.context, t.org, t.wg, ref)).toEqual(data);
    expect(t.context.refreshLog).not.toHaveBeenCalled();
  });

  it('chooses a new object for a file without one', async () => {
    const t = await taskWorld();
    const targets: string[] = [];
    t.client.request = async (method, target) => {
      targets.push(`${method} ${target}`);
      return {};
    };
    const ref = await uploadAttachment(
      t.context,
      t.org,
      t.wg,
      pendingFile('f', 'a.png', 'image/png', new Uint8Array([1])),
    );
    expect(ref.object).not.toBe(OBJECT);
    expect(targets).toEqual([
      `PUT /v1/workgroups/${t.wg}/attachments/${ref.object}/chunks/0`,
      `POST /v1/workgroups/${t.wg}/attachments/${ref.object}/commit`,
    ]);
  });

  it('asks the log and wrap jobs once for a key it does not hold, then refuses', async () => {
    const t = await taskWorld();
    hub(t);
    const ref = await uploadAttachment(
      t.context,
      t.org,
      t.wg,
      pendingFile('f', 'a.png', 'image/png', new Uint8Array([1])),
      OBJECT,
    );
    const later = { ...ref, epoch: 2 };
    await expect(downloadAttachment(t.context, t.org, t.wg, later)).rejects.toMatchObject({ code: 'epoch_stale' });
    expect(t.context.refreshLog).toHaveBeenCalledTimes(1);
    // the jobs bring the key: the download goes on (and the other key fails to open the chunk)
    t.context.refreshLog.mockImplementation(async () => {
      addKeys(t.store, t.org, new Map([[epochKey(WG, t.wg, 3), Secret.of(new Uint8Array(32).fill(9))]]));
      return true;
    });
    await expect(downloadAttachment(t.context, t.org, t.wg, { ...ref, epoch: 3 })).rejects.toMatchObject({
      code: 'decrypt_failed',
    });
  });

  it('refuses a chunk answer without its ciphertext', async () => {
    const t = await taskWorld();
    hub(t);
    const ref = await uploadAttachment(
      t.context,
      t.org,
      t.wg,
      pendingFile('f', 'a.png', 'image/png', new Uint8Array([1])),
      OBJECT,
    );
    t.client.on(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`, () => ({ ct: 5 }));
    await expect(downloadAttachment(t.context, t.org, t.wg, ref)).rejects.toMatchObject({ code: 'unavailable' });
    t.client.on(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`, () => null);
    await expect(downloadAttachment(t.context, t.org, t.wg, ref)).rejects.toMatchObject({ code: 'unavailable' });
  });
});
