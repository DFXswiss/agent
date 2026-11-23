// Waiting for device responses: pulls at start, on `device_response` frames and on the fallback timer, one read at a
// time, stops when the flow ended, reports failed reads and writes nothing after a sign-out.
import { describe, expect, it, vi } from 'vitest';
import { HubError } from '../../../src/core/net/signedFetch';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import type { PreparedRequest, RequestView } from '../../../src/core/requests/client';
import { ended, watchRequest } from '../../../src/core/requests/watch';
import { noteResponse } from '../../../src/core/store/slices/requests';
import { taskWorld } from '../tasks/world';

const ID = '0190f000-0000-7000-8000-0000000000e1';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const prepared = {
  id: ID,
  kind: 'github.connect',
  follows: '',
  device: { device: DEVICE, kem: '', sig: '' },
  envelope: { header: {}, ct: '', sig: '' },
} as PreparedRequest;

const view = (state: string, responses: unknown[] = []) => ({
  request_id: ID,
  kind: 'github.connect',
  device_id: DEVICE,
  state,
  created_at: '2026-11-21T12:00:00.000Z',
  expires_at: '2026-11-21T12:15:00.000Z',
  responses,
});

function manualTimer() {
  const ticks: (() => void)[] = [];
  return {
    ticks,
    stopped: vi.fn(),
    timer: { start: (tick: () => void) => (ticks.push(tick), 7), stop: (handle: unknown) => void handle },
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('watchRequest', () => {
  it('reads at start, on frames and on ticks until the hub ends the request', async () => {
    const t = await taskWorld();
    const answers = [view('pending'), view('delivered'), view('expired')];
    t.client.on(`GET /v1/device-requests/${ID}`, () => answers.shift() ?? view('expired'));
    const seen: string[] = [];
    const m = manualTimer();
    const stop = vi.fn();
    const timer = { ...m.timer, stop };
    watchRequest(t.context, prepared, { onView: (v) => seen.push(v.state), timer });
    await settle();
    applyFrame(t.store, 'device_response', { request_id: ID, seq: 1 }, 0, frameHandlers());
    await settle();
    m.ticks[0]!();
    await settle();
    expect(seen).toEqual(['pending', 'delivered', 'expired']);
    expect(stop).toHaveBeenCalledWith(7);
    m.ticks[0]!();
    await settle();
    expect(seen).toHaveLength(3);
  });

  it('runs one read at a time and repeats once for reads asked meanwhile', async () => {
    const t = await taskWorld();
    let release: () => void = () => undefined;
    let calls = 0;
    t.client.on(`GET /v1/device-requests/${ID}`, async () => {
      calls += 1;
      if (calls === 1) await new Promise<void>((resolve) => (release = resolve));
      return view('pending');
    });
    const m = manualTimer();
    const seen: string[] = [];
    const stop = watchRequest(t.context, prepared, { onView: (v) => seen.push(v.state), timer: m.timer });
    await settle();
    m.ticks[0]!();
    m.ticks[0]!();
    release();
    await settle();
    await settle();
    expect(calls).toBe(2);
    stop();
    stop();
    m.ticks[0]!();
    await settle();
    expect(calls).toBe(2);
  });

  it('reports failed reads and keeps watching; nothing after a sign-out or a stop', async () => {
    const t = await taskWorld();
    let fail = true;
    t.client.on(`GET /v1/device-requests/${ID}`, () => {
      if (fail) throw new HubError('rate_limited', 429);
      return view('pending');
    });
    const m = manualTimer();
    const errors: string[] = [];
    const seen: string[] = [];
    const stop = watchRequest(t.context, prepared, {
      onView: (v) => seen.push(v.state),
      onError: (code) => errors.push(code),
      timer: m.timer,
    });
    await settle();
    expect(errors).toEqual(['rate_limited']);
    fail = false;
    t.store.reset();
    m.ticks[0]!();
    await settle();
    expect(seen).toEqual([]);
    stop();
    // a failure after the stop is not reported, and without onError nothing breaks
    fail = true;
    const other = watchRequest(t.context, prepared, { onView: () => undefined, timer: m.timer });
    await settle();
    other();
    const late = watchRequest(t.context, prepared, {
      onView: () => undefined,
      onError: (c) => errors.push(c),
      timer: m.timer,
    });
    late();
    await settle();
    expect(errors).toEqual(['rate_limited']);
  });

  it('uses the interval timer by default', async () => {
    vi.useFakeTimers();
    try {
      const t = await taskWorld();
      t.client.on(`GET /v1/device-requests/${ID}`, () => view('pending'));
      const stop = watchRequest(t.context, prepared, { onView: () => undefined, pollMs: 100 });
      await vi.advanceTimersByTimeAsync(250);
      expect(t.client.of(`GET /v1/device-requests/${ID}`)).toHaveLength(3);
      stop();
      await vi.advanceTimersByTimeAsync(250);
      expect(t.client.of(`GET /v1/device-requests/${ID}`)).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('ended', () => {
  it('is over on an ending hub state or a final verified response', () => {
    const base: RequestView = { id: ID, state: 'delivered', expiresAt: 0, responses: [] };
    expect(ended(base)).toBe(false);
    expect(ended({ ...base, state: 'answered' })).toBe(true);
    expect(ended({ ...base, responses: [{ seq: 1, status: 'unverifiable', code: 'x' }] })).toBe(false);
    expect(
      ended({ ...base, responses: [{ seq: 1, status: 'verified', final: true, at: 0, body: { step: 'done' } }] }),
    ).toBe(true);
  });
});

describe('requests slice', () => {
  it('keeps the newest announced seq and ignores malformed frames', async () => {
    const t = await taskWorld();
    noteResponse(t.store, ID, 2);
    noteResponse(t.store, ID, 1);
    applyFrame(t.store, 'device_response', { request_id: ID, seq: 'x' }, 0, frameHandlers());
    applyFrame(t.store, 'device_response', null, 0, frameHandlers());
    expect(t.store.getState().requests.seqs).toEqual({ [ID]: 2 });
  });
});
