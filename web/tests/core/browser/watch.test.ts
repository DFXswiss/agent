// Watching a session's platform browser: the watch with the bandwidth wish, the record read at start and on every
// (re)connect, live entries and frames, the newest open browser session from the store, refusal and stop.
import { describe, expect, it, vi } from 'vitest';
import type { OpenedFrame } from '../../../src/core/browser/live';
import { BrowserWatch, type BrowserContext } from '../../../src/core/browser/watch';
import type { BrowserListener } from '../../../src/core/browser/watchlist';
import { createStore } from '../../../src/core/store';
import { putEvents } from '../../../src/core/store/slices/events';
import { BROWSER, ORG, read, RecordHub, SESSION, settle, state, stored, TASK, WG } from './world';

function setup() {
  const store = createStore();
  const hub = new RecordHub([read(1)]);
  const listeners: BrowserListener[] = [];
  const handle = { setKbps: vi.fn(), release: vi.fn() };
  const opener = vi.fn(() => async (input: unknown): Promise<OpenedFrame> => {
    const { browser, seq } = input as { browser: string; seq: number };
    return { browser, seq, ts: 0, jpeg: new Uint8Array([seq]) };
  });
  const watchFn = vi.fn((_org: string, _session: string, listener: BrowserListener) => {
    listeners.push(listener);
    return handle;
  });
  const context: BrowserContext = {
    ...hub.context(),
    store,
    watches: (create) => {
      create(() => undefined);
      return { watch: watchFn };
    },
    opener,
  };
  const watch = new BrowserWatch(context, { org: ORG, wg: WG, task: TASK, session: SESSION }, 250);
  return { store, hub, listeners, handle, opener, watchFn, watch };
}

describe('BrowserWatch', () => {
  it('watches with the wish, reads the record, takes live entries and frames and stops', async () => {
    const t = setup();
    expect(t.opener).toHaveBeenCalledWith(ORG, WG, SESSION);
    const views: unknown[] = [];
    const unsubscribe = t.watch.subscribe(() => views.push(t.watch.view));
    t.watch.setKbps(1000);
    expect(t.handle.setKbps).not.toHaveBeenCalled();
    t.watch.start();
    expect(t.watchFn).toHaveBeenCalledWith(ORG, SESSION, expect.anything(), 1000);
    await settle();
    expect(t.watch.view.record.entries).toHaveLength(1);
    t.watch.setKbps(3000);
    expect(t.handle.setKbps).toHaveBeenCalledWith(3000);
    const [listener] = t.listeners;
    listener!.action({ scope_id: WG, ...stored(read(2)) });
    await settle();
    expect(t.watch.view.record.entries).toHaveLength(2);
    // a reconnect reads on from the last position
    t.hub.put([read(3)]);
    listener!.resumed();
    await settle();
    expect(t.watch.view.record.entries).toHaveLength(3);
    // a frame is shown only while the store has an open browser session
    listener!.live({ session_id: SESSION, frame: { browser: BROWSER, seq: 1 } });
    await settle();
    expect(t.watch.view.live.frame).toBeNull();
    putEvents(t.store, WG, [read(10, { kind: 'browser.state', body: state() })]);
    listener!.live({ session_id: SESSION, frame: { browser: BROWSER, seq: 1 } });
    await settle();
    expect(t.watch.view.live.frame?.seq).toBe(1);
    expect(t.watch.view.refused).toBe(false);
    listener!.refused();
    expect(t.watch.view.refused).toBe(true);
    expect(views.length).toBeGreaterThan(3);
    unsubscribe();
    t.watch.stop();
    t.watch.stop();
    expect(t.handle.release).toHaveBeenCalledTimes(1);
    t.watch.setKbps(250);
    expect(t.handle.setKbps).toHaveBeenCalledTimes(1);
  });

  it('opens live frames with the keys of the store without an opener of its own', async () => {
    const t = setup();
    const context: BrowserContext = { ...t.hub.context(), store: t.store, watches: () => ({ watch: t.watchFn }) };
    const watch = new BrowserWatch(context, { org: ORG, wg: WG, task: TASK, session: SESSION }, 1000);
    putEvents(t.store, WG, [read(10, { kind: 'browser.state', body: state() })]);
    await watch.live.receive({ session_id: SESSION, frame: { browser: BROWSER, seq: 1 } });
    expect(watch.live.state.error).toBe('header_invalid');
  });
});
