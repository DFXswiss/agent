// The live wiring: cursors restored and saved (throttled), events to the reader, state to the store, `log` frames to
// the log job only when the head changed, `wraps` frames to the wrap job, other frames to `onFrame`.
import { describe, expect, it } from 'vitest';
import { announcesChange, connectLive, SAVE_INTERVAL_MS } from '../../../src/core/net/live';
import type { CursorMap } from '../../../src/core/net/cursors';
import type { StoredEvent } from '../../../src/core/net/sse';
import { createStore } from '../../../src/core/store';
import { setLog } from '../../../src/core/store/slices/orgs';
import type { VerifiedLog } from '../../../src/core/log/verify';
import { FakeLiveClient, ManualTimers, settle, stored } from './stream';

const ORG = '01930000-0000-7000-8000-0000000000aa';
const SCOPE = '01930000-0000-7000-8000-00000000000a';

function setup(initial: CursorMap = {}) {
  const store = createStore();
  const client = new FakeLiveClient();
  const timers = new ManualTimers();
  const saved: CursorMap[] = [];
  const triggers: string[] = [];
  const ingested: [string, StoredEvent[]][] = [];
  const frames: [string, unknown][] = [];
  let failSave = false;
  const wiring = {
    store,
    client,
    timers,
    org: ORG,
    random: () => 0.5,
    now: () => timers.now,
    runner: { trigger: async (trigger: string, org: string) => triggers.push(`${trigger} ${org}`) > 0 },
    reader: {
      ingest: async (_org: string, scope: string, items: readonly StoredEvent[]) =>
        void ingested.push([scope, [...items]]),
    },
    persist: {
      loadCursors: async () => initial,
      saveCursors: async (_org: string, cursors: CursorMap) => {
        if (failSave) throw new Error('quota');
        saved.push(cursors);
      },
    },
    onFrame: (type: string, data: unknown) => void frames.push([type, data]),
  };
  return { store, client, timers, saved, triggers, ingested, frames, wiring, failSaves: () => (failSave = true) };
}

describe('live wiring', () => {
  it('connects with the stored cursors and routes events, frames and state', async () => {
    const t = setup({ [SCOPE]: 1 });
    const { channel } = await connectLive({
      ...t.wiring,
      terminals: ['s'],
      browsers: ['b'],
      browserKbps: 3000,
      monitoring: true,
    });
    await settle();
    expect(t.client.last.target).toContain('browser=b');
    expect(t.client.last.target).toContain('browser_kbps=3000');
    expect(t.client.last.target).toContain(`cursors=${SCOPE}%3A1`);
    expect(t.client.last.target).toContain('terminal=s');
    expect(t.client.last.target).toContain('monitoring=1');
    const c = t.client.last;
    c.frame('hello', { stream_id: ORG, heads: { [SCOPE]: 1 }, hub_time: '' });
    c.frame('event', { scope_id: SCOPE, ...stored(2) });
    c.frame('wraps', {});
    c.frame('notification', { id: 'n' });
    await settle();
    expect(t.store.getState().connection.status).toBe('live');
    expect(t.ingested.map(([scope, items]) => [scope, items.map((i) => i.scope_seq)])).toEqual([[SCOPE, [2]]]);
    expect(t.triggers).toEqual([`wraps ${ORG}`]);
    // `hello` reaches the slices after the channel processed it (the hub clock, WEB-04)
    expect(t.frames).toEqual([
      ['hello', { stream_id: ORG, heads: { [SCOPE]: 1 }, hub_time: '' }],
      ['notification', { id: 'n' }],
    ]);
    channel.stop();
    expect(t.store.getState().connection.status).toBe('stopped');
  });

  it('saves cursors at most every SAVE_INTERVAL_MS and survives a failed save', async () => {
    const t = setup();
    await connectLive(t.wiring);
    await settle();
    const c = t.client.last;
    c.frame('hello', { stream_id: ORG, heads: { [SCOPE]: 0 }, hub_time: '' });
    c.frame('event', { scope_id: SCOPE, ...stored(1) });
    c.frame('event', { scope_id: SCOPE, ...stored(2) });
    await settle();
    expect(t.saved).toEqual([]);
    t.timers.advance(SAVE_INTERVAL_MS);
    await settle();
    expect(t.saved).toEqual([{ [SCOPE]: 2 }]);
    t.failSaves();
    c.frame('event', { scope_id: SCOPE, ...stored(3) });
    await settle();
    t.timers.advance(SAVE_INTERVAL_MS);
    await settle();
    expect(t.saved).toHaveLength(1);
  });

  it('stops persisting cursors on stop', async () => {
    const t = setup();
    const live = await connectLive(t.wiring);
    await settle();
    t.client.last.frame('hello', { stream_id: ORG, heads: { [SCOPE]: 0 }, hub_time: '' });
    t.client.last.frame('event', { scope_id: SCOPE, ...stored(1) });
    await settle();
    live.stop();
    t.timers.advance(SAVE_INTERVAL_MS);
    await settle();
    expect(t.saved).toEqual([]);
    live.stop();
  });

  it('triggers the log job only for a changed head', async () => {
    const t = setup();
    setLog(t.store, ORG, { head: { seq: 3, hash: 'h3' } } as unknown as VerifiedLog);
    await connectLive(t.wiring);
    await settle();
    const c = t.client.last;
    c.frame('log', { org_id: ORG, head: { seq: 3, hash: 'h3' } });
    c.frame('log', { org_id: ORG, head: { seq: 3, hash: 'other' } });
    c.frame('log', { org_id: ORG, head: { seq: 4, hash: 'h4' } });
    c.frame('log', { org_id: 'o2', head: { seq: 0, hash: 'x' } });
    c.frame('log', { org_id: ORG });
    c.frame('log', 'x');
    await settle();
    expect(t.triggers).toEqual([`log ${ORG}`, `log ${ORG}`, 'log o2']);
    expect(announcesChange(t.store, { org_id: 7, head: {} })).toBeNull();
  });

  it('works with the defaults (system timers, no frame listener)', async () => {
    const t = setup();
    const { timers: _timers, onFrame: _onFrame, random: _random, now: _now, ...wiring } = t.wiring;
    const { channel } = await connectLive(wiring);
    await settle();
    t.client.last.frame('task', {});
    t.client.last.frame('hello', { stream_id: ORG, heads: { [SCOPE]: 4 }, hub_time: '' });
    await settle();
    expect(t.store.getState().connection.status).toBe('live');
    channel.stop();
  });
});
