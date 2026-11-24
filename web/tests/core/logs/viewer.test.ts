// The log viewer's state (ADR-078 section 3): loading, the first page, paging within the log, late pages dropped,
// failures with their code, the download, and nothing reported after closing.
import { describe, expect, it, vi } from 'vitest';
import { concat } from '../../../src/core/encoding/bytes';
import { CHUNK_BYTES } from '../../../src/core/crypto/runlog';
import { HubError } from '../../../src/core/net/signedFetch';
import { LogViewer } from '../../../src/core/logs/viewer';
import { ascii, buildLog, logPath, logWorld } from './world';

/** A route answer that waits for `release`. */
function gate(answer: () => unknown) {
  let release!: () => void;
  const opened = new Promise<void>((resolve) => (release = resolve));
  return { release, handler: async () => (await opened, answer()) };
}

describe('LogViewer', () => {
  it('opens the first page, moves within the log and reports every change', async () => {
    const w = await logWorld();
    const output = ascii(2 * CHUNK_BYTES + 5);
    const built = await buildLog(w, output);
    const viewer = new LogViewer(w.t.context, built.source);
    const listener = vi.fn();
    const unsubscribe = viewer.subscribe(listener);
    expect(viewer.snapshot()).toMatchObject({ status: 'loading', text: null, code: null });
    await viewer.open();
    expect(viewer.snapshot()).toMatchObject({ status: 'ready', chunks: 3, index: 0, truncated: false });
    expect(viewer.snapshot().text).toHaveLength(CHUNK_BYTES);
    expect(listener).toHaveBeenCalled();
    await viewer.go(99);
    expect(viewer.snapshot()).toMatchObject({ index: 2, text: 'aaaaa' });
    await viewer.go(-3);
    expect(viewer.snapshot().index).toBe(0);
    unsubscribe();
    listener.mockClear();
    await viewer.go(1);
    expect(listener).not.toHaveBeenCalled();
  });

  it('drops the answer of a page the user already left, also a failed one', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(3 * CHUNK_BYTES));
    const viewer = new LogViewer(w.t.context, built.source);
    await viewer.open();
    const answer = w.t.client.routes.get(logPath('/chunks/1'))!;
    const slow = gate(() => answer(undefined, ''));
    w.t.client.on(logPath('/chunks/1'), slow.handler);
    const late = viewer.go(1);
    await viewer.go(0);
    slow.release();
    await late;
    expect(viewer.snapshot()).toMatchObject({ index: 0, code: null });
    expect(viewer.snapshot().text).toHaveLength(CHUNK_BYTES);
    const failing = gate(() => {
      throw new HubError('unavailable', 503);
    });
    w.t.client.on(logPath('/chunks/2'), failing.handler);
    const lost = viewer.go(2);
    await viewer.go(0);
    failing.release();
    await lost;
    expect(viewer.snapshot()).toMatchObject({ index: 0, code: null });
  });

  it('shows why the log or a page could not be read', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(CHUNK_BYTES + 1));
    w.t.client.on(logPath('/chunks/1'), () => {
      throw new HubError('rate_limited', 429);
    });
    const viewer = new LogViewer(w.t.context, built.source);
    await viewer.open();
    await viewer.go(1);
    expect(viewer.snapshot()).toMatchObject({ status: 'ready', index: 1, text: null, code: 'rate_limited' });
    w.t.client.on(logPath(), () => ({}));
    const broken = new LogViewer(w.t.context, built.source);
    await broken.open();
    expect(broken.snapshot()).toMatchObject({ status: 'failed', code: 'header_invalid' });
  });

  it('downloads every chunk, and reports a failed download', async () => {
    const w = await logWorld();
    const output = ascii(CHUNK_BYTES + 9);
    const built = await buildLog(w, output, { bytes: CHUNK_BYTES * 4 });
    const viewer = new LogViewer(w.t.context, built.source);
    await viewer.open();
    expect(viewer.snapshot().truncated).toBe(true);
    const parts = viewer.download();
    expect(viewer.snapshot().downloading).toBe(true);
    expect(concat(...(await parts)!)).toEqual(output);
    expect(viewer.snapshot()).toMatchObject({ downloading: false, downloadCode: null });
    w.t.client.on(logPath('/chunks/1'), () => {
      throw new HubError('unavailable', 503);
    });
    expect(await viewer.download()).toBeNull();
    expect(viewer.snapshot()).toMatchObject({ downloading: false, downloadCode: 'unavailable' });
  });

  it('reports nothing after it was closed', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100));
    const viewer = new LogViewer(w.t.context, built.source);
    const opening = viewer.open();
    viewer.close();
    await opening;
    expect(viewer.snapshot().status).toBe('loading');
    const reopened = new LogViewer(w.t.context, built.source);
    await reopened.open();
    const download = reopened.download();
    reopened.close();
    expect(await download).toBeNull();
    expect(reopened.snapshot().downloading).toBe(true);
  });
});
