// Watched terminals of the live channel (03-api.md section 5, 06-web.md section 5.5): `setTerminals` reopens a running
// connection with the newest three sessions, and a stream the hub refuses because of a watched session reconnects
// without them and reports them with `terminals_refused`.
import { describe, expect, it } from 'vitest';
import { Cursors } from '../../../src/core/net/cursors';
import { LiveChannel, MAX_TERMINALS } from '../../../src/core/net/sse';
import { HubError } from '../../../src/core/net/signedFetch';
import { FakeLiveClient, ManualTimers, settle } from './stream';

const ORG = '01930000-0000-7000-8000-0000000000aa';

function setup(terminals?: string[]) {
  const client = new FakeLiveClient();
  const timers = new ManualTimers();
  const frames: [string, unknown][] = [];
  const channel = new LiveChannel({
    client,
    org: ORG,
    cursors: new Cursors({}),
    timers,
    random: () => 0.5,
    now: () => timers.now,
    ...(terminals !== undefined && { terminals }),
    handlers: {
      events: () => undefined,
      frame: (type, data) => {
        frames.push([type, data]);
      },
      state: () => undefined,
    },
  });
  const watched = () => new URL(client.last.target, 'https://hub.example').searchParams.getAll('terminal');
  return { client, timers, channel, frames, watched };
}

describe('watched terminals', () => {
  it('keeps the newest three of the initial list', async () => {
    const t = setup(['a', 'b', 'c', 'd']);
    expect(MAX_TERMINALS).toBe(3);
    expect(t.channel.terminals).toEqual(['b', 'c', 'd']);
    t.channel.start();
    await settle();
    expect(t.watched()).toEqual(['b', 'c', 'd']);
  });

  it('reopens a running connection when the list changes, not when it stays the same', async () => {
    const t = setup();
    t.channel.start();
    await settle();
    expect(t.client.connections).toHaveLength(1);
    t.channel.setTerminals(['s1', 's1', 's2', 's3', 's4']);
    await settle();
    expect(t.client.connections).toHaveLength(2);
    expect(t.client.connections[0]!.signal.aborted).toBe(true);
    expect(t.watched()).toEqual(['s2', 's3', 's4']);
    t.channel.setTerminals(['s2', 's3', 's4']);
    await settle();
    expect(t.client.connections).toHaveLength(2);
    t.channel.setTerminals(['s2', 's3', 's5']);
    await settle();
    expect(t.watched()).toEqual(['s2', 's3', 's5']);
    t.channel.setTerminals([]);
    await settle();
    expect(t.watched()).toEqual([]);
    expect(t.client.connections).toHaveLength(4);
  });

  it('only stores the list while the channel is not running', async () => {
    const t = setup();
    t.channel.setTerminals(['s1']);
    expect(t.client.connections).toHaveLength(0);
    t.channel.start();
    await settle();
    expect(t.watched()).toEqual(['s1']);
    t.channel.stop();
    t.channel.setTerminals(['s2']);
    await settle();
    expect(t.client.connections).toHaveLength(1);
  });

  it('drops the newest watched session when the hub refuses the stream, one at a time, then gives up', async () => {
    const t = setup(['ok', 'gone']);
    t.client.refuse = new HubError('not_found', 404);
    t.channel.start();
    await settle();
    expect(t.frames).toEqual([['terminals_refused', { sessions: ['gone'] }]]);
    expect(t.channel.terminals).toEqual(['ok']);
    expect(t.client.connections).toHaveLength(1);
    expect(t.watched()).toEqual(['ok']);
    expect(t.channel.state.status).toBe('connecting');
    // refused again (the org itself, say): the last terminal goes, then the refusal is fatal
    const s = setup(['only']);
    s.client.refuse = new HubError('not_found', 404);
    s.channel.start();
    await settle();
    s.client.last.end();
    await settle();
    expect(s.frames).toEqual([['terminals_refused', { sessions: ['only'] }]]);
  });

  it('treats not_found without terminals as fatal as before', async () => {
    const t = setup();
    t.client.refuse = new HubError('not_found', 404);
    t.channel.start();
    await settle();
    expect(t.frames).toEqual([]);
    expect(t.channel.state).toMatchObject({ status: 'stopped', error: 'not_found' });
  });
});
