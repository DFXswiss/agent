// Watched browsers of the live channel (03-api.md section 5, 06-web.md section 5.4b): `browser=` with one
// `browser_kbps`, `setBrowsers` reopens a running connection with the newest three sessions and the rate, a
// refused stream drops the newest watched browser first, and the browser frames go to the frame handler.
import { describe, expect, it } from 'vitest';
import { Cursors } from '../../../src/core/net/cursors';
import { LiveChannel, MAX_BROWSERS } from '../../../src/core/net/sse';
import { HubError } from '../../../src/core/net/signedFetch';
import { FakeLiveClient, ManualTimers, settle } from './stream';

const ORG = '01930000-0000-7000-8000-0000000000aa';

function setup(options: { browsers?: string[]; browserKbps?: number; terminals?: string[] } = {}) {
  const client = new FakeLiveClient();
  const timers = new ManualTimers();
  const frames: [string, unknown][] = [];
  const events: unknown[] = [];
  const channel = new LiveChannel({
    client,
    org: ORG,
    cursors: new Cursors({}),
    timers,
    random: () => 0.5,
    now: () => timers.now,
    ...options,
    handlers: {
      events: (_scope, items) => {
        events.push(...items);
      },
      frame: (type, data) => {
        frames.push([type, data]);
      },
      state: () => undefined,
    },
  });
  const query = () => new URL(client.last.target, 'https://hub.example').searchParams;
  return { client, timers, channel, frames, events, query };
}

describe('watched browsers', () => {
  it('asks for the newest three with one rate, and for no rate without a browser', async () => {
    const t = setup({ browsers: ['a', 'b', 'c', 'd'], browserKbps: 50 });
    expect(MAX_BROWSERS).toBe(3);
    expect(t.channel.browsers).toEqual(['b', 'c', 'd']);
    expect(t.channel.browserKbps).toBe(50);
    t.channel.start();
    await settle();
    expect(t.query().getAll('browser')).toEqual(['b', 'c', 'd']);
    expect(t.query().get('browser_kbps')).toBe('50');
    const none = setup();
    expect(none.channel.browserKbps).toBe(1000);
    none.channel.start();
    await settle();
    expect(none.query().has('browser_kbps')).toBe(false);
  });

  it('reopens a running connection when the list or the rate changes, not otherwise', async () => {
    const t = setup();
    t.channel.setBrowsers(['s0'], 250);
    expect(t.client.connections).toHaveLength(0);
    t.channel.start();
    await settle();
    expect(t.query().getAll('browser')).toEqual(['s0']);
    t.channel.setBrowsers(['s0'], 250);
    await settle();
    expect(t.client.connections).toHaveLength(1);
    t.channel.setBrowsers(['s0'], 20_000);
    await settle();
    expect(t.query().get('browser_kbps')).toBe('20000');
    t.channel.setBrowsers(['s1', 's1', 's2'], 20_000);
    await settle();
    expect(t.query().getAll('browser')).toEqual(['s1', 's2']);
    t.channel.setBrowsers(['s1', 's3'], 20_000);
    await settle();
    expect(t.query().getAll('browser')).toEqual(['s1', 's3']);
    expect(t.client.connections).toHaveLength(4);
  });

  it('drops the newest watched browser of a refused stream before any terminal', async () => {
    const t = setup({ browsers: ['ok', 'gone'], terminals: ['term'] });
    t.client.refuse = new HubError('not_found', 404);
    t.channel.start();
    await settle();
    expect(t.frames).toEqual([['browsers_refused', { sessions: ['gone'] }]]);
    expect(t.channel.browsers).toEqual(['ok']);
    expect(t.channel.terminals).toEqual(['term']);
    expect(t.query().getAll('browser')).toEqual(['ok']);
  });

  it('hands browser frames to the frame handler, never to the events', async () => {
    const t = setup({ browsers: ['s'] });
    t.channel.start();
    await settle();
    const action = { scope_id: 'wg', scope_seq: 9, event: { header: {} } };
    t.client.last.frame('browser_action', action);
    t.client.last.frame('browser_live', { session_id: 's', frame: {} });
    await settle();
    expect(t.frames).toEqual([
      ['browser_action', action],
      ['browser_live', { session_id: 's', frame: {} }],
    ]);
    expect(t.events).toEqual([]);
  });
});
