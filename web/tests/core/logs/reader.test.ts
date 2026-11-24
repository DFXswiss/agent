// Reading an encrypted log (ADR-080 section 3): the manifest must be well formed, name what the event named and be
// signed by an org device; the chunks are opened with the workgroup key of the manifest's epoch, have fixed sizes,
// and the download checks size and digest. The pages decode UTF-8 across chunk borders and keep a small cache.
import { describe, expect, it } from 'vitest';
import { concat, utf8 } from '../../../src/core/encoding/bytes';
import { CHUNK_BYTES } from '../../../src/core/crypto/runlog';
import { HubError } from '../../../src/core/net/signedFetch';
import { CACHED_CHUNKS, LogPages, locateEvent, openLog, readAll, readChunk } from '../../../src/core/logs/reader';
import { putEvents, type EventEntry } from '../../../src/core/store/slices/events';
import { actors } from '../log/world';
import { ascii, buildLog, logPath, logWorld, REF, TASK } from './world';

const OTHER = '0190f000-0000-7000-8000-00000000ffff';

describe('openLog', () => {
  it('checks the manifest, its signature and derives the chunk key', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(CHUNK_BYTES + 100));
    const log = await openLog(w.t.context, built.source);
    expect(log).toMatchObject({ chunks: 2, stored: CHUNK_BYTES + 100, bytes: CHUNK_BYTES + 100, truncated: false });
    expect(log.manifest).toEqual(built.manifest);
    expect(w.t.client.of(logPath())).toHaveLength(1);
    expect(await readChunk(w.t.context, log, 1)).toEqual(ascii(CHUNK_BYTES + 100).subarray(CHUNK_BYTES));
  });

  it('reports a log that keeps only the last part as truncated', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(500), { bytes: 9000 });
    expect(await openLog(w.t.context, built.source)).toMatchObject({ stored: 500, bytes: 9000, truncated: true });
  });

  it.each([
    ['another object', { manifest: { object: OTHER } }, {}],
    ['another task', { manifest: { task: OTHER } }, {}],
    ['another workgroup', { manifest: { wg: OTHER } }, {}],
    ['another run', {}, { ref: 'tr-00000000000f' }],
    ['other full bytes', {}, { bytes: 1 }],
    ['other stored bytes', {}, { stored_bytes: 1 }],
  ])('refuses a manifest that names %s than the event', async (_, options, change) => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100), options);
    const { ref, ...log } = change as { ref?: string; bytes?: number; stored_bytes?: number };
    const source = { ...built.source, ref: ref ?? REF, log: { ...built.source.log, ...log } };
    await expect(openLog(w.t.context, source)).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('refuses an answer without a manifest', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100));
    w.t.client.on(logPath(), () => 'nothing');
    await expect(openLog(w.t.context, built.source)).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('refuses an author that is no device of the org, and an org without a verified log', async () => {
    const w = await logWorld();
    const [stranger] = await actors('stranger');
    const byUser = await buildLog(w, ascii(100), { manifest: { author: w.t.ada.id }, signer: w.t.ada });
    await expect(openLog(w.t.context, byUser.source)).rejects.toMatchObject({ code: 'author_not_writer' });
    const unknown = await buildLog(w, ascii(100), { manifest: { author: stranger.id }, signer: stranger });
    await expect(openLog(w.t.context, unknown.source)).rejects.toMatchObject({ code: 'author_not_writer' });
    const otherOrg = await buildLog(w, ascii(100), { manifest: { org: OTHER } });
    await expect(openLog(w.t.context, otherOrg.source)).rejects.toMatchObject({ code: 'author_not_writer' });
  });

  it('refuses a signature of another key', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100), { signer: w.t.ada });
    await expect(openLog(w.t.context, built.source)).rejects.toMatchObject({ code: 'signature_invalid' });
  });

  it('waits for the key of an epoch the user does not hold', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100), { manifest: { epoch: w.epoch + 5 } });
    await expect(openLog(w.t.context, built.source)).rejects.toMatchObject({ code: 'keys_pending' });
  });
});

describe('readChunk and readAll', () => {
  it('reads every chunk and checks size and digest', async () => {
    const w = await logWorld();
    const output = ascii(2 * CHUNK_BYTES + 7);
    const built = await buildLog(w, output);
    const log = await openLog(w.t.context, built.source);
    expect(concat(...(await readAll(w.t.context, log)))).toEqual(output);
    expect(w.t.client.of(logPath('/chunks/2'))).toHaveLength(1);
  });

  it('refuses a chunk of the wrong length and an answer without a chunk', async () => {
    const w = await logWorld();
    const built = await buildLog(w, new Uint8Array(0), { pieces: [ascii(10), ascii(CHUNK_BYTES)] });
    const log = await openLog(w.t.context, built.source);
    await expect(readChunk(w.t.context, log, 0)).rejects.toMatchObject({ code: 'header_invalid' });
    w.t.client.on(logPath('/chunks/1'), () => ({ nothing: true }));
    await expect(readChunk(w.t.context, log, 1)).rejects.toMatchObject({ code: 'header_invalid' });
    w.t.client.on(logPath('/chunks/1'), () => 'nothing');
    await expect(readChunk(w.t.context, log, 1)).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('refuses a download whose size or digest differs from the manifest', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(300));
    const log = await openLog(w.t.context, built.source);
    const sized = { ...log, manifest: { ...log.manifest, size: (log.manifest.size as number) + 1 } };
    await expect(readAll(w.t.context, sized)).rejects.toMatchObject({ code: 'header_invalid' });
    const other = { ...log, manifest: { ...log.manifest, digest: 'A'.repeat(43) } };
    await expect(readAll(w.t.context, other)).rejects.toMatchObject({ code: 'header_invalid' });
  });
});

describe('LogPages', () => {
  it('completes a character that straddles two chunks and leaves its rest out of the next page', async () => {
    const w = await logWorld();
    const text = `${'a'.repeat(CHUNK_BYTES - 1)}äb€`;
    // the last chunk ends inside "€": there is no next chunk to complete it from
    const output = utf8(text).subarray(0, utf8(text).length - 1);
    const built = await buildLog(w, output);
    const pages = new LogPages(w.t.context, await openLog(w.t.context, built.source));
    expect((await pages.page(0)).endsWith('aä')).toBe(true);
    expect(await pages.page(1)).toBe('b�');
    // chunk 1 was read once for both pages
    expect(w.t.client.of(logPath('/chunks/1'))).toHaveLength(1);
  });

  it('leaves out leading continuation bytes of the first page of a truncated log', async () => {
    const w = await logWorld();
    const built = await buildLog(w, utf8('ä-tail').subarray(1), { bytes: 100 });
    const pages = new LogPages(w.t.context, await openLog(w.t.context, built.source));
    expect(await pages.page(0)).toBe('-tail');
  });

  it(`keeps the last ${CACHED_CHUNKS} chunks and reads a failed chunk again`, async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(6 * CHUNK_BYTES));
    const pages = new LogPages(w.t.context, await openLog(w.t.context, built.source));
    for (let index = 0; index < 6; index += 1) await pages.page(index);
    await pages.page(5);
    await pages.page(0);
    expect(w.t.client.of(logPath('/chunks/5'))).toHaveLength(1);
    expect(w.t.client.of(logPath('/chunks/0'))).toHaveLength(2);
    let fail = true;
    const answer = w.t.client.routes.get(logPath('/chunks/3'))!;
    w.t.client.on(logPath('/chunks/3'), (body, target) => {
      if (fail) throw new HubError('unavailable', 503);
      return answer(body, target);
    });
    for (const index of [1, 2]) await pages.page(index);
    await expect(pages.page(3)).rejects.toMatchObject({ code: 'unavailable' });
    fail = false;
    const third = ascii(6 * CHUNK_BYTES).subarray(3 * CHUNK_BYTES, 4 * CHUNK_BYTES);
    expect(await pages.page(3)).toBe(new TextDecoder().decode(third));
  });
});

describe('locateEvent', () => {
  it('finds the workgroup and task of the verified event with the same body', async () => {
    const w = await logWorld();
    const body = { run_id: REF };
    const entry = (seq: number, value: unknown, status: EventEntry['status'] = 'verified'): EventEntry => ({
      org: w.t.org,
      scopeId: w.t.wg,
      scopeSeq: seq,
      receivedAt: null,
      event: {},
      status,
      code: null,
      header: { task: TASK },
      body: value,
    });
    putEvents(w.t.store, w.t.wg, [entry(1, body), entry(2, { ...body }), entry(3, body, 'locked')]);
    putEvents(w.t.store, OTHER, [entry(1, { other: true })]);
    const state = w.t.store.getState();
    expect(locateEvent(state, 1, body)).toEqual({ wg: w.t.wg, task: TASK });
    expect(locateEvent(state, 2, body)).toBeNull();
    expect(locateEvent(state, 3, body)).toBeNull();
    expect(locateEvent(state, 9, body)).toBeNull();
  });
});
