// The monitoring flag of the live channel (03-api.md section 5, 06-web.md section 9.1): `monitoring=1` adds `metrics`
// frames only while the monitoring screen is open, so `setMonitoring` reopens a running connection when it changes.
import { describe, expect, it } from 'vitest';
import { Cursors } from '../../../src/core/net/cursors';
import { LiveChannel } from '../../../src/core/net/sse';
import { FakeLiveClient, ManualTimers, settle } from './stream';

const ORG = '01930000-0000-7000-8000-0000000000aa';

function setup(monitoring?: boolean) {
  const client = new FakeLiveClient();
  const timers = new ManualTimers();
  const channel = new LiveChannel({
    client,
    org: ORG,
    cursors: new Cursors({}),
    timers,
    random: () => 0.5,
    now: () => timers.now,
    ...(monitoring !== undefined && { monitoring }),
    handlers: { events: () => undefined, frame: () => undefined, state: () => undefined },
  });
  const flag = () => new URL(client.last.target, 'https://hub.example').searchParams.get('monitoring');
  return { client, channel, flag };
}

describe('monitoring flag', () => {
  it('starts with the option and reopens a running connection only when the flag changes', async () => {
    const t = setup(true);
    expect(t.channel.monitoring).toBe(true);
    t.channel.start();
    await settle();
    expect(t.flag()).toBe('1');
    t.channel.setMonitoring(true);
    await settle();
    expect(t.client.connections).toHaveLength(1);
    t.channel.setMonitoring(false);
    await settle();
    expect(t.client.connections).toHaveLength(2);
    expect(t.client.connections[0]!.signal.aborted).toBe(true);
    expect(t.flag()).toBeNull();
    expect(t.channel.monitoring).toBe(false);
  });

  it('only stores the flag while the channel is not running', async () => {
    const t = setup();
    expect(t.channel.monitoring).toBe(false);
    t.channel.setMonitoring(true);
    expect(t.client.connections).toHaveLength(0);
    t.channel.start();
    await settle();
    expect(t.flag()).toBe('1');
  });
});
