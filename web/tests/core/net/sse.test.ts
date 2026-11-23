// The SSE parser with arbitrarily split chunks and the live channel: hello/resync pulls, de-duplication, gap repair,
// heartbeat, reconnect with backoff and jitter (manual timers), fatal errors, offline/online, stop.
import { describe, expect, it } from 'vitest';
import { utf8 } from '../../../src/core/encoding/bytes';
import { Cursors } from '../../../src/core/net/cursors';
import {
  BACKOFF_MAX_MS,
  backoff,
  HEARTBEAT_TIMEOUT_MS,
  LiveChannel,
  type LiveState,
  MAX_BACKFILL,
  REPAIR_DELAY_MS,
  SseParser,
  type SseItem,
  type StoredEvent,
} from '../../../src/core/net/sse';
import { HubError } from '../../../src/core/net/signedFetch';
import { FakeLiveClient, ManualTimers, settle, stored } from './stream';

const SCOPE_A = '01930000-0000-7000-8000-00000000000a';
const SCOPE_B = '01930000-0000-7000-8000-00000000000b';
const ORG = '01930000-0000-7000-8000-0000000000aa';

const SAMPLE =
  ': heartbeat\r\n\r\n' +
  'event: hello\r\ndata: {"a":1}\r\n\r\n' +
  'event:event\ndata:{"b":\ndata: 2}\n\n' +
  'id: 7\nretry: 10\nunknown\nevent: x\n\n' +
  'data\n\n' +
  'data: plain\rdata: two\r\r' +
  'event: log\ndata: {"c":"ü"}\n\n';

const EXPECTED: SseItem[] = [
  { kind: 'comment' },
  { kind: 'frame', event: 'hello', data: '{"a":1}' },
  { kind: 'frame', event: 'event', data: '{"b":\n2}' },
  { kind: 'frame', event: 'message', data: '' },
  { kind: 'frame', event: 'message', data: 'plain\ntwo' },
  { kind: 'frame', event: 'log', data: '{"c":"ü"}' },
];

describe('SSE parser', () => {
  it('parses a whole stream', () => {
    expect(new SseParser().push(SAMPLE)).toEqual(EXPECTED);
  });

  it('gives the same frames for every split into two and into single characters', () => {
    for (let cut = 0; cut <= SAMPLE.length; cut += 1) {
      const parser = new SseParser();
      expect([...parser.push(SAMPLE.slice(0, cut)), ...parser.push(SAMPLE.slice(cut))]).toEqual(EXPECTED);
    }
    const parser = new SseParser();
    expect([...SAMPLE].flatMap((char) => parser.push(char))).toEqual(EXPECTED);
  });

  it('keeps an unfinished frame until its blank line', () => {
    const parser = new SseParser();
    expect(parser.push('event: a\ndata: 1\n')).toEqual([]);
    expect(parser.push('\n')).toEqual([{ kind: 'frame', event: 'a', data: '1' }]);
  });
});

describe('backoff', () => {
  it('doubles from 1 s to 30 s with ±20 % jitter', () => {
    expect([1, 2, 3, 4, 5, 6, 7, 40].map((n) => backoff(n, 0.5))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
    expect(backoff(1, 0)).toBe(1000);
    expect(backoff(3, 0)).toBe(3200);
    expect(backoff(3, 1)).toBe(4800);
    expect(backoff(6, 1)).toBe(BACKOFF_MAX_MS);
  });
});

function setup(
  options: { cursors?: Record<string, number>; terminals?: string[]; monitoring?: boolean; now?: boolean } = {},
) {
  const client = new FakeLiveClient();
  const timers = new ManualTimers();
  const cursors = new Cursors(options.cursors ?? {});
  const delivered: [string, StoredEvent[]][] = [];
  const frames: [string, unknown][] = [];
  const states: LiveState[] = [];
  const channel = new LiveChannel({
    client,
    org: ORG,
    cursors,
    timers,
    random: () => 0.5,
    ...(options.now !== false && { now: () => timers.now }),
    ...(options.terminals !== undefined && { terminals: options.terminals }),
    ...(options.monitoring !== undefined && { monitoring: options.monitoring }),
    handlers: {
      events: (scope, items) => {
        delivered.push([scope, items]);
      },
      frame: (type, data) => {
        frames.push([type, data]);
      },
      state: (state) => states.push(state),
    },
  });
  const seqs = () => delivered.flatMap(([scope, items]) => items.map((item) => `${scope.slice(-1)}${item.scope_seq}`));
  return { client, timers, cursors, channel, delivered, frames, states, seqs, status: () => channel.state.status };
}

const hello = (heads: Record<string, number>) => ({ stream_id: ORG, heads, hub_time: '2026-11-21T12:00:00.000Z' });
const frame = (scope: string, seq: number) => ({ scope_id: scope, ...stored(seq) });

describe('live channel', () => {
  it('builds the stream target from org, terminals, cursors and monitoring', async () => {
    const t = setup({ cursors: { [SCOPE_B]: 3, [SCOPE_A]: 5 }, terminals: ['s1', 's2'], monitoring: true });
    t.channel.start();
    await settle();
    const url = new URL(t.client.last.target, 'https://hub.example');
    expect(url.pathname).toBe('/v1/stream');
    expect(url.searchParams.get('org')).toBe(ORG);
    expect(url.searchParams.getAll('terminal')).toEqual(['s1', 's2']);
    expect(url.searchParams.get('cursors')).toBe(`${SCOPE_A}:5,${SCOPE_B}:3`);
    expect(url.searchParams.get('monitoring')).toBe('1');
    const plain = setup();
    plain.channel.start();
    await settle();
    expect(plain.client.last.target).toBe(`/v1/stream?org=${ORG}`);
  });

  it('pulls behind cursors after hello, sets new scopes to their head and only then is live', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 2 } });
    t.client.pull = (_scope, after) => ({ events: [stored(after + 1), stored(after + 2)], next_after: null, head: 4 });
    t.channel.start();
    await settle();
    expect(t.status()).toBe('connecting');
    t.client.last.frame('hello', hello({ [SCOPE_A]: 4, [SCOPE_B]: 9 }));
    await settle();
    expect(t.client.pulls).toEqual([`${SCOPE_A}:2`]);
    expect(t.seqs()).toEqual(['a3', 'a4']);
    expect(t.cursors.snapshot()).toEqual({ [SCOPE_A]: 4, [SCOPE_B]: 9 });
    expect(t.states.map((s) => s.status)).toEqual(['connecting', 'syncing', 'live']);
  });

  it('ignores malformed heads and does not pull a scope that is up to date', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 4 } });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 4, [SCOPE_B]: -1 }));
    t.client.last.frame('hello', { heads: 'nope' });
    await settle();
    expect(t.client.pulls).toEqual([]);
    expect(t.status()).toBe('live');
  });

  it('delivers in order, drops duplicates and repairs gaps with one delayed pull per scope', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    // the hub holds 2..7; seq 4 is an unwatched terminal chunk the pull does not return
    t.client.pull = (_scope, after) => ({
      events: [7, 3, 2, 5, 6, 99].filter((seq) => seq > after).map((seq) => stored(seq)),
      next_after: null,
      head: 7,
    });
    t.channel.start();
    await settle();
    const c = t.client.last;
    c.frame('hello', hello({ [SCOPE_A]: 1 }));
    c.frame('event', frame(SCOPE_A, 2));
    c.frame('event', frame(SCOPE_A, 2));
    c.frame('event', frame(SCOPE_A, 5));
    c.frame('event', frame(SCOPE_A, 6));
    c.frame('event', frame(SCOPE_A, 5));
    await settle();
    expect(t.seqs()).toEqual(['a2', 'a5', 'a6']);
    expect(t.cursors.get(SCOPE_A)).toBe(2);
    expect(t.client.pulls).toEqual([]);
    t.timers.advance(REPAIR_DELAY_MS);
    await settle();
    expect(t.client.pulls).toEqual([`${SCOPE_A}:2`]);
    expect(t.seqs()).toEqual(['a2', 'a5', 'a6', 'a3', 'a7']);
    expect(t.cursors.get(SCOPE_A)).toBe(7);
    c.frame('event', frame(SCOPE_A, 7));
    c.frame('event', frame(SCOPE_A, 8));
    await settle();
    expect(t.seqs().slice(-1)).toEqual(['a8']);
    expect(t.timers.pending).toEqual([HEARTBEAT_TIMEOUT_MS]);
  });

  it('delivers a watched terminal chunk at once although the gap pull never returns it', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 5 } });
    t.client.pull = () => ({ events: [], next_after: null, head: 7 });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 5 }));
    t.client.last.frame('terminal', frame(SCOPE_A, 7));
    await settle();
    expect(t.seqs()).toEqual(['a7']);
    t.timers.advance(REPAIR_DELAY_MS);
    await settle();
    t.client.last.frame('terminal', frame(SCOPE_A, 7));
    await settle();
    expect(t.seqs()).toEqual(['a7']);
    expect(t.cursors.get(SCOPE_A)).toBe(7);
  });

  it('pulls an unseen scope from the start when its first frame leaves a gap', async () => {
    const t = setup();
    t.client.pull = () => ({ events: [stored(1), stored(2), stored(3)], next_after: null, head: 3 });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({}));
    t.client.last.frame('event', frame(SCOPE_B, 3));
    await settle();
    t.timers.advance(REPAIR_DELAY_MS);
    await settle();
    expect(t.client.pulls).toEqual([`${SCOPE_B}:0`]);
    expect(t.seqs()).toEqual(['b3', 'b1', 'b2']);
  });

  it('repairs again when frames arrived beyond the head a pull reported', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    let head = 3;
    t.client.pull = (_scope, after) => ({
      events: [2, 3, 4].filter((seq) => seq > after && seq <= head).map((seq) => stored(seq)),
      next_after: null,
      head,
    });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 1 }));
    t.client.last.frame('event', frame(SCOPE_A, 5));
    await settle();
    t.timers.advance(REPAIR_DELAY_MS);
    await settle();
    expect(t.cursors.get(SCOPE_A)).toBe(3);
    head = 5;
    t.timers.advance(REPAIR_DELAY_MS);
    await settle();
    expect(t.seqs()).toEqual(['a5', 'a2', 'a3', 'a4']);
    expect(t.cursors.get(SCOPE_A)).toBe(5);
  });

  it('cancels the repair of a scope the hub no longer shows', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 1 }));
    t.client.last.frame('event', frame(SCOPE_A, 3));
    await settle();
    t.client.pull = () => {
      throw new HubError('not_found', 404);
    };
    t.client.last.frame('resync', { reason: 'hub_restart' });
    await settle();
    expect(t.cursors.has(SCOPE_A)).toBe(false);
    expect(t.timers.pending).toEqual([HEARTBEAT_TIMEOUT_MS]);
  });

  it('starts over at the head when the hub reports a head below the cursor (restored hub)', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 9 } });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 4 }));
    t.client.last.frame('event', frame(SCOPE_A, 5));
    await settle();
    expect(t.cursors.get(SCOPE_A)).toBe(5);
    expect(t.seqs()).toEqual(['a5']);
  });

  it('drops a pending repair when the connection closes and repairs after the next hello', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    t.client.pull = (_scope, after) => ({ events: after < 3 ? [stored(2), stored(3)] : [], next_after: null, head: 3 });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 1 }));
    t.client.last.frame('event', frame(SCOPE_A, 3));
    await settle();
    t.client.last.end();
    await settle();
    expect(t.timers.pending).toEqual([1000]);
    t.timers.advance(1000);
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 3 }));
    await settle();
    expect(t.seqs()).toEqual(['a3', 'a2']);
    expect(t.cursors.get(SCOPE_A)).toBe(3);
  });

  it('pages a pull, skips ahead beyond the backfill limit and keeps the hub head as cursor', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    const head = 2 + MAX_BACKFILL + 3;
    t.client.pull = (_scope, after) =>
      after < head - 2
        ? { events: [stored(after + 1), { ...stored(after + 2), received_at: undefined }], next_after: after + 2, head }
        : { events: [stored(after + 1)], next_after: null, head };
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: head }));
    await settle(60);
    expect(t.client.pulls.slice(0, 3)).toEqual([
      `${SCOPE_A}:1`,
      `${SCOPE_A}:${head - MAX_BACKFILL}`,
      `${SCOPE_A}:${head - MAX_BACKFILL + 2}`,
    ]);
    expect(t.seqs()[0]).toBe(`a${head - MAX_BACKFILL + 1}`);
    expect(t.delivered[0]![1][1]!.received_at).toBeNull();
    expect(t.cursors.get(SCOPE_A)).toBe(head);
    expect(t.status()).toBe('live');
  });

  it('forgets a scope the hub no longer shows', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    t.client.pull = () => {
      throw new HubError('not_found', 404);
    };
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 3 }));
    await settle();
    expect(t.cursors.has(SCOPE_A)).toBe(false);
    expect(t.status()).toBe('live');
  });

  it('re-pulls every cursor on resync before it is live again', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1, [SCOPE_B]: 7 } });
    t.client.pull = (scope, after) => ({ events: [], next_after: null, head: scope === SCOPE_A ? 2 : after });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({}));
    await settle();
    t.client.last.frame('resync', { reason: 'buffer_overflow' });
    await settle();
    expect(t.client.pulls).toEqual([`${SCOPE_A}:1`, `${SCOPE_B}:7`]);
    expect(t.states.slice(-2).map((s) => s.status)).toEqual(['syncing', 'live']);
    expect(t.cursors.get(SCOPE_A)).toBe(2);
    // the slices learn of it like of a hello (rows whose frames the hub dropped are loaded again; KOR-05, A-23)
    expect(t.frames.map(([type]) => type)).toEqual(['hello', 'resync']);
    expect(t.frames[1]).toEqual(['resync', { reason: 'buffer_overflow' }]);
  });

  it('passes other frames on and ignores malformed ones', async () => {
    const t = setup();
    t.channel.start();
    await settle();
    const c = t.client.last;
    c.frame('task', { task_id: 't' });
    c.send('event: log\ndata: {broken\n\n');
    c.frame('event', { scope_id: SCOPE_A, scope_seq: 0, event: {} });
    c.frame('event', { scope_id: SCOPE_A, scope_seq: 1, event: 'x' });
    c.frame('event', 'x');
    c.send(': ping\n\n');
    await settle();
    expect(t.frames).toEqual([['task', { task_id: 't' }]]);
    expect(t.delivered).toEqual([]);
  });

  it('decodes UTF-8 split across chunks', async () => {
    const t = setup();
    t.channel.start();
    await settle();
    const bytes = utf8('event: task\ndata: {"t":"ü€"}\n\n');
    const c = t.client.last;
    for (const byte of bytes) c.send(new Uint8Array([byte]));
    await settle();
    expect(t.frames).toEqual([['task', { t: 'ü€' }]]);
  });

  it('reconnects with backoff after a missed heartbeat, an ended stream and a failed pull', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    t.channel.start();
    await settle();
    t.client.last.send(': hi\n\n');
    await settle();
    t.timers.advance(HEARTBEAT_TIMEOUT_MS - 1);
    await settle();
    expect(t.status()).toBe('connecting');
    t.timers.advance(1);
    await settle();
    expect(t.channel.state).toMatchObject({
      status: 'reconnecting',
      attempt: 1,
      error: 'unavailable',
      retryAt: t.timers.now + 1000,
    });
    expect(t.timers.pending).toEqual([1000]);
    t.timers.advance(1000);
    await settle();
    expect(t.client.connections).toHaveLength(2);
    t.client.last.end();
    await settle();
    expect(t.channel.state).toMatchObject({ status: 'reconnecting', attempt: 2 });
    expect(t.timers.pending).toEqual([2000]);
    t.timers.advance(2000);
    await settle();
    t.client.pull = () => {
      throw new HubError('unavailable', 503);
    };
    t.client.last.frame('hello', hello({ [SCOPE_A]: 5 }));
    t.client.last.frame('task', {});
    await settle();
    expect(t.channel.state).toMatchObject({ status: 'reconnecting', attempt: 3 });
    expect(t.frames).toEqual([]);
    t.timers.advance(4000);
    await settle();
    t.client.pull = (_s, after) => ({ events: [], next_after: null, head: after });
    t.client.last.frame('hello', hello({}));
    await settle();
    expect(t.channel.state).toEqual({ status: 'live', retryAt: null, attempt: 0, error: null });
    expect(t.client.connections).toHaveLength(4);
  });

  it('reconnects after a malformed pull answer', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    t.client.pull = () => ({ events: 'x' });
    t.channel.start();
    await settle();
    t.client.last.frame('hello', hello({ [SCOPE_A]: 2 }));
    await settle();
    expect(t.channel.state).toMatchObject({ status: 'reconnecting', error: 'unavailable' });
  });

  it('reconnects when the stream request fails, and stops on fatal errors', async () => {
    const t = setup();
    t.client.refuse = new HubError('rate_limited', 429);
    t.channel.start();
    await settle();
    expect(t.channel.state).toMatchObject({ status: 'reconnecting', error: 'rate_limited' });
    t.client.refuse = new HubError('session_expired', 401);
    t.timers.advance(1000);
    await settle();
    expect(t.channel.state).toMatchObject({ status: 'stopped', error: 'session_expired' });
    expect(t.timers.pending).toEqual([]);
    t.channel.start();
    await settle();
    expect(t.status()).toBe('connecting');
    const other = setup();
    other.client.stream = () => Promise.reject(new Error('boom'));
    other.channel.start();
    await settle();
    expect(other.channel.state).toMatchObject({ status: 'reconnecting', error: 'unavailable' });
  });

  it('treats a response without a body as an ended stream', async () => {
    const t = setup();
    t.client.stream = async () => new Response(null, { status: 200 });
    t.channel.start();
    await settle();
    expect(t.status()).toBe('reconnecting');
  });

  it('does not start twice, stops cleanly and ignores late results of a stopped connection', async () => {
    const t = setup({ cursors: { [SCOPE_A]: 1 } });
    let release: (value: unknown) => void = () => undefined;
    t.client.pull = () => new Promise((resolve) => (release = resolve));
    t.channel.start();
    t.channel.start();
    await settle();
    expect(t.client.connections).toHaveLength(1);
    t.client.last.frame('hello', hello({ [SCOPE_A]: 2 }));
    await settle();
    t.channel.stop();
    release({ events: [stored(2)], next_after: null, head: 2 });
    await settle();
    expect(t.delivered).toEqual([]);
    expect(t.status()).toBe('stopped');
    expect(t.client.last.signal.aborted).toBe(true);
    t.channel.stop();
    expect(t.status()).toBe('stopped');
  });

  it('goes offline without retrying and reconnects at once when online again', async () => {
    const t = setup();
    t.channel.online();
    t.channel.offline();
    expect(t.status()).toBe('offline');
    t.channel.start();
    await settle();
    expect(t.client.connections).toHaveLength(0);
    t.channel.online();
    await settle();
    expect(t.client.connections).toHaveLength(1);
    t.channel.online();
    await settle();
    expect(t.client.connections).toHaveLength(1);
    t.client.refuse = null;
    t.client.last.end();
    await settle();
    t.channel.offline();
    expect(t.timers.pending).toEqual([]);
    expect(t.status()).toBe('offline');
    t.channel.stop();
    t.channel.online();
    await settle();
    expect(t.client.connections).toHaveLength(1);
    expect(t.status()).toBe('stopped');
  });

  it('uses the system clock, timers and randomness by default', async () => {
    const client = new FakeLiveClient();
    const states: LiveState[] = [];
    const channel = new LiveChannel({
      client,
      org: ORG,
      cursors: new Cursors(),
      handlers: { events: () => undefined, frame: () => undefined, state: (s) => states.push(s) },
    });
    client.refuse = new HubError('unavailable', 503);
    const before = Date.now();
    channel.start();
    await settle();
    expect(channel.state.status).toBe('reconnecting');
    expect(channel.state.retryAt!).toBeGreaterThanOrEqual(before + 800);
    channel.stop();
  });
});
