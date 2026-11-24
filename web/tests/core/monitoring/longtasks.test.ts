// U-WEB-ui-health (KOR-35, ADR-086 section 5): the long-task early warning of the web client – blocks over 200 ms
// counted per view type (count, blocked time, bucket of the longest block) from `PerformanceObserver('longtask')` or,
// without it, from the lateness of a 1 s timer; at most one anonymous report an hour and only with something counted,
// also when the page is hidden, the first one at a random time within the hour; no cookies, no referrer; failed reports
// keep their counters; the pages of a browser share the hourly limit and a pool of unreported counters in localStorage,
// so each counter is reported once however many tabs are open or reloaded.
import { describe, expect, it, vi } from 'vitest';
import type { PrefStorage } from '../../../src/core/account/prefs';
import {
  addBlock,
  addCounts,
  browserMonitorOptions,
  bucketOf,
  CHECK_INTERVAL_MS,
  type Counts,
  loadSaved,
  longTaskObserver,
  type LongTaskMonitorOptions,
  MAX_BLOCKED_MS,
  MAX_COUNT,
  MAX_GAP_MS,
  type MonitorWindow,
  type ObserverClass,
  SEND_INTERVAL_MS,
  startLongTaskMonitor,
  STORAGE_KEY,
  TICK_MS,
} from '../../../src/core/monitoring/longtasks';
import type { ViewName } from '../../../src/core/monitoring/views';

const T0 = Date.parse('2026-11-24T10:00:00.000Z');
const HUB = 'https://hub.example.org';
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

class MemoryStorage implements PrefStorage {
  readonly items = new Map<string, string>();
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}

/** A monitor on fake timers, clock, page and fetch; `block` feeds the observer source (null: fallback timer). */
function harness(
  input: {
    observer?: boolean;
    storage?: PrefStorage | null;
    fetch?: typeof globalThis.fetch;
    now?: number;
    random?: number;
  } = {},
) {
  const state = { now: input.now ?? T0, view: 'task.chat' as ViewName };
  const intervals = new Map<number, { fn: () => void; ms: number }>();
  let handles = 0;
  let onBlock: ((ms: number) => void) | null = null;
  const stopObserver = vi.fn();
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' });
  const page = new EventTarget();
  const fetch = vi.fn<typeof globalThis.fetch>(input.fetch ?? (async () => new Response(null, { status: 204 })));
  const storage = input.storage === undefined ? new MemoryStorage() : input.storage;
  const options: LongTaskMonitorOptions = {
    origin: HUB,
    version: '1.4.0',
    fetch,
    view: () => state.view,
    observe: (callback) => {
      if (input.observer === false) return null;
      onBlock = callback;
      return stopObserver;
    },
    now: () => state.now,
    setInterval: (fn, ms) => {
      intervals.set(++handles, { fn, ms });
      return handles;
    },
    clearInterval: (handle) => intervals.delete(handle as number),
    page,
    document: doc,
    storage,
    random: () => input.random ?? 0,
  };
  const stop = startLongTaskMonitor(options);
  const run = (ms: number) => [...intervals.values()].filter((entry) => entry.ms === ms).forEach((entry) => entry.fn());
  return {
    state,
    intervals,
    fetch,
    storage,
    stop,
    stopObserver,
    block: (ms: number) => onBlock!(ms),
    /** Advances the clock and runs the hourly check. */
    check: (advance = 0) => {
      state.now += advance;
      run(CHECK_INTERVAL_MS);
    },
    /** Advances the clock by `TICK_MS + late` and runs the fallback tick. */
    tick: (late = 0) => {
      state.now += TICK_MS + late;
      run(TICK_MS);
    },
    setVisibility: (visibility: 'visible' | 'hidden') => {
      doc.visibilityState = visibility;
      doc.dispatchEvent(new Event('visibilitychange'));
    },
    pagehide: () => page.dispatchEvent(new Event('pagehide')),
    sent: (index = 0) => {
      const [url, init] = fetch.mock.calls[index]!;
      return { url, init: init!, body: JSON.parse(init!.body as string) as { version: string; views: Counts } };
    },
  };
}

describe('counters', () => {
  it('buckets a block by the largest lower bound it reached', () => {
    expect(
      [0, 201, 499, 500, 999, 1000, 1999, 2000, 4999, 5000, 9999, 10000, 29999, 30000, 600000].map(bucketOf),
    ).toEqual([0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]);
  });

  it('counts only blocks over 200 ms and keeps the longest bucket', () => {
    const counts: Counts = {};
    addBlock(counts, 'home', 150);
    addBlock(counts, 'home', 200);
    expect(counts).toEqual({});
    addBlock(counts, 'home', 1200.6);
    addBlock(counts, 'home', 250);
    addBlock(counts, 'audit', 6000);
    expect(counts).toEqual({
      home: { count: 2, blocked_ms: 1451, max_bucket: 2 },
      audit: { count: 1, blocked_ms: 6000, max_bucket: 4 },
    });
  });

  it('bounds the counters', () => {
    const counts: Counts = {};
    addCounts(counts, 'home', { count: MAX_COUNT, blocked_ms: MAX_BLOCKED_MS, max_bucket: 1 });
    addCounts(counts, 'home', { count: 5, blocked_ms: 5000, max_bucket: 0 });
    expect(counts.home).toEqual({ count: MAX_COUNT, blocked_ms: MAX_BLOCKED_MS, max_bucket: 1 });
  });
});

describe('longTaskObserver', () => {
  function fakeObserver(supported: readonly string[] | undefined) {
    const calls = { observe: vi.fn(), disconnect: vi.fn(), callback: null as null | ((list: unknown) => void) };
    const Observer = class {
      static readonly supportedEntryTypes = supported;
      constructor(callback: (list: unknown) => void) {
        calls.callback = callback;
      }
      observe = calls.observe;
      disconnect = calls.disconnect;
    } as unknown as ObserverClass;
    return { Observer, calls };
  }

  it('is unsupported without the observer or without the longtask entry type', () => {
    expect(longTaskObserver(undefined)(() => undefined)).toBeNull();
    expect(longTaskObserver(fakeObserver(undefined).Observer)(() => undefined)).toBeNull();
    expect(longTaskObserver(fakeObserver(['mark', 'measure']).Observer)(() => undefined)).toBeNull();
  });

  it('forwards the durations of longtask entries and disconnects on stop', () => {
    const { Observer, calls } = fakeObserver(['longtask']);
    const durations: number[] = [];
    const stop = longTaskObserver(Observer)((ms) => durations.push(ms))!;
    expect(calls.observe).toHaveBeenCalledWith({ type: 'longtask', buffered: true });
    calls.callback!({ getEntries: () => [{ duration: 60 }, { duration: 320 }] });
    expect(durations).toEqual([60, 320]);
    stop();
    expect(calls.disconnect).toHaveBeenCalledOnce();
  });
});

describe('startLongTaskMonitor', () => {
  it('reports the counters per view anonymously, at most once an hour', async () => {
    const t = harness();
    t.check();
    expect(t.fetch).not.toHaveBeenCalled();
    t.block(240);
    t.state.view = 'session.terminal';
    t.block(2600);
    t.block(120);
    t.check();
    expect(t.fetch).toHaveBeenCalledOnce();
    const { url, init, body } = t.sent();
    expect(url).toBe(`${HUB}/v1/telemetry/ui`);
    expect(init).toMatchObject({
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-store',
      keepalive: true,
    });
    expect(body).toEqual({
      version: '1.4.0',
      views: {
        'task.chat': { count: 1, blocked_ms: 240, max_bucket: 0 },
        'session.terminal': { count: 1, blocked_ms: 2600, max_bucket: 3 },
      },
    });
    await flush();
    t.block(700);
    t.check(SEND_INTERVAL_MS - 1);
    expect(t.fetch).toHaveBeenCalledOnce();
    t.check(1);
    expect(t.fetch).toHaveBeenCalledTimes(2);
    expect(t.sent(1).body.views).toEqual({ 'session.terminal': { count: 1, blocked_ms: 700, max_bucket: 1 } });
    await flush();
    t.check(SEND_INTERVAL_MS);
    expect(t.fetch).toHaveBeenCalledTimes(2);
  });

  it('keeps the counters of a failed report for the next one', async () => {
    let answer: () => Promise<Response> = async () => {
      throw new TypeError('offline');
    };
    const t = harness({ fetch: () => answer() });
    t.block(300);
    t.check();
    await flush();
    answer = async () => new Response(null, { status: 503 });
    t.block(400);
    t.check(SEND_INTERVAL_MS);
    expect(t.sent(1).body.views).toEqual({ 'task.chat': { count: 2, blocked_ms: 700, max_bucket: 0 } });
    await flush();
    answer = async () => new Response(null, { status: 204 });
    t.check(SEND_INTERVAL_MS);
    expect(t.sent(2).body.views).toEqual({ 'task.chat': { count: 2, blocked_ms: 700, max_bucket: 0 } });
    await flush();
    t.check(SEND_INTERVAL_MS);
    expect(t.fetch).toHaveBeenCalledTimes(3);
  });

  it('measures event-loop gaps of a 1 s timer without the longtask observer', () => {
    const t = harness({ observer: false });
    expect([...t.intervals.values()].map((entry) => entry.ms).sort((a, b) => a - b)).toEqual([
      TICK_MS,
      CHECK_INTERVAL_MS,
    ]);
    t.tick();
    t.tick(150);
    t.tick(200);
    t.tick(350);
    t.tick(MAX_GAP_MS);
    t.tick(MAX_GAP_MS + 1);
    t.setVisibility('hidden');
    t.tick(30_000);
    t.state.now += 45_000;
    t.setVisibility('visible');
    t.tick();
    t.check();
    expect(t.sent().body.views).toEqual({
      'task.chat': { count: 2, blocked_ms: 350 + MAX_GAP_MS, max_bucket: 6 },
    });
    t.stop();
    expect(t.intervals.size).toBe(0);
  });

  it('reports when the page is hidden and keeps the state across a reload', () => {
    const storage = new MemoryStorage();
    const first = harness({ storage });
    first.block(300);
    first.setVisibility('hidden');
    expect(first.fetch).toHaveBeenCalledOnce();
    first.setVisibility('visible');
    first.block(900);
    first.pagehide();
    expect(first.fetch).toHaveBeenCalledOnce();
    expect(JSON.parse(storage.getItem(STORAGE_KEY)!)).toEqual({
      sent: T0,
      views: { 'task.chat': { count: 1, blocked_ms: 900, max_bucket: 1 } },
    });
    first.stop();
    expect(first.stopObserver).toHaveBeenCalledOnce();
    expect(first.intervals.size).toBe(0);
    first.block(300);
    first.pagehide();
    first.setVisibility('hidden');
    expect(first.fetch).toHaveBeenCalledOnce();

    const second = harness({ storage, now: T0 + 60_000 });
    second.block(250);
    second.check();
    expect(second.fetch).not.toHaveBeenCalled();
    second.check(SEND_INTERVAL_MS);
    expect(second.sent().body.views).toEqual({ 'task.chat': { count: 2, blocked_ms: 1150, max_bucket: 1 } });
  });

  it('sends the first report at a random time within the hour after the page loaded', () => {
    const t = harness({ random: 0.5 });
    t.block(300);
    t.check();
    t.check(SEND_INTERVAL_MS / 2 - 1);
    t.setVisibility('hidden');
    expect(t.fetch).not.toHaveBeenCalled();
    t.setVisibility('visible');
    t.check(1);
    expect(t.sent().body.views).toEqual({ 'task.chat': { count: 1, blocked_ms: 300, max_bucket: 0 } });
  });

  it('reports each counter once and honours the limit of the whole browser across tabs', async () => {
    const storage = new MemoryStorage();
    const a = harness({ storage, random: 0.25 });
    const b = harness({ storage, random: 0.5 });
    a.block(300);
    // a is hidden before its report is due: its counters go into the pool
    a.setVisibility('hidden');
    expect(JSON.parse(storage.getItem(STORAGE_KEY)!)).toEqual({
      sent: 0,
      views: { 'task.chat': { count: 1, blocked_ms: 300, max_bucket: 0 } },
    });
    a.setVisibility('visible');
    // a reload of a (a third page) does not report the pool again on its own
    const reload = harness({ storage, random: 0.75 });
    b.block(500);
    a.state.now = b.state.now = reload.state.now = T0 + SEND_INTERVAL_MS / 2;
    b.check();
    expect(b.sent().body.views).toEqual({ 'task.chat': { count: 2, blocked_ms: 800, max_bucket: 1 } });
    expect(JSON.parse(storage.getItem(STORAGE_KEY)!)).toEqual({ sent: T0 + SEND_INTERVAL_MS / 2, views: {} });
    await flush();
    // the other pages see the report of b: nothing more within the hour, then only new counters
    a.block(400);
    a.check();
    reload.check(SEND_INTERVAL_MS / 4);
    a.setVisibility('hidden');
    expect(a.fetch).not.toHaveBeenCalled();
    expect(reload.fetch).not.toHaveBeenCalled();
    reload.check(SEND_INTERVAL_MS);
    expect(reload.sent().body.views).toEqual({ 'task.chat': { count: 1, blocked_ms: 400, max_bucket: 0 } });
    b.check(SEND_INTERVAL_MS * 2);
    expect(b.fetch).toHaveBeenCalledOnce();
  });

  it('keeps the pool when it cannot be emptied and reports only its own counters then', () => {
    const storage = new MemoryStorage();
    storage.setItem(
      STORAGE_KEY,
      JSON.stringify({ sent: 0, views: { home: { count: 1, blocked_ms: 300, max_bucket: 0 } } }),
    );
    const readOnly: PrefStorage = {
      getItem: (key) => storage.getItem(key),
      setItem: () => {
        throw new DOMException('full', 'QuotaExceededError');
      },
      removeItem: () => undefined,
    };
    const idle = harness({ storage: readOnly });
    idle.check();
    // the pool alone is due, but it cannot be taken: nothing is sent
    expect(idle.fetch).not.toHaveBeenCalled();
    const t = harness({ storage: readOnly });
    t.block(250);
    t.check();
    expect(t.sent().body.views).toEqual({ 'task.chat': { count: 1, blocked_ms: 250, max_bucket: 0 } });
  });

  it('works without a storage and with a refusing one', () => {
    const none = harness({ storage: null });
    none.block(300);
    none.pagehide();
    expect(none.fetch).toHaveBeenCalledOnce();
    const refusing: PrefStorage = {
      getItem: () => {
        throw new DOMException('denied', 'SecurityError');
      },
      setItem: () => {
        throw new DOMException('full', 'QuotaExceededError');
      },
      removeItem: () => undefined,
    };
    const t = harness({ storage: refusing });
    t.block(300);
    expect(() => t.pagehide()).not.toThrow();
    expect(t.fetch).toHaveBeenCalledOnce();
  });
});

describe('loadSaved', () => {
  const stored = (value: unknown) => {
    const storage = new MemoryStorage();
    storage.setItem(STORAGE_KEY, typeof value === 'string' ? value : JSON.stringify(value));
    return loadSaved(storage, T0);
  };
  const empty = { sent: 0, views: {} };

  it('drops what is unreadable or malformed', () => {
    expect(loadSaved(null, T0)).toEqual(empty);
    expect(loadSaved(new MemoryStorage(), T0)).toEqual(empty);
    expect(stored('{')).toEqual(empty);
    expect(stored([1])).toEqual(empty);
    expect(stored({ sent: T0 + 1, views: [] })).toEqual(empty);
    expect(stored({ sent: 1.5, views: null })).toEqual(empty);
    expect(stored({ sent: -1 })).toEqual(empty);
    const good = { count: 1, blocked_ms: 300, max_bucket: 0 };
    expect(
      stored({
        sent: T0,
        views: {
          home: good,
          'task.nope': good,
          audit: { ...good, max_bucket: 7 },
          settings: { ...good, max_bucket: 'x' },
          members: { ...good, count: -1 },
          devices: { ...good, blocked_ms: 'x' },
          monitoring: 7,
        },
      }),
    ).toEqual({ sent: T0, views: { home: good } });
  });
});

describe('browserMonitorOptions', () => {
  function fakeWindow(storage: 'ok' | 'throws') {
    const win = Object.assign(new EventTarget(), {
      location: { pathname: '/o/org-1/s/sess-1', search: '?tab=chat' },
      document: Object.assign(new EventTarget(), { visibilityState: 'visible' }),
      setInterval: vi.fn((_fn: () => void, _ms: number) => 7),
      clearInterval: vi.fn(),
    });
    const store = new MemoryStorage();
    Object.defineProperty(win, 'localStorage', {
      get: () => {
        if (storage === 'throws') throw new DOMException('denied', 'SecurityError');
        return store;
      },
    });
    return { win: win as typeof win & MonitorWindow, store };
  }

  it('takes the view from the location and uses the window timers, events and storage', () => {
    const { win, store } = fakeWindow('ok');
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response(null, { status: 204 }));
    const options = browserMonitorOptions(win, { origin: HUB, version: '1.4.0', fetch });
    expect(options).toMatchObject({ origin: HUB, version: '1.4.0', fetch, page: win, document: win.document });
    expect(options.storage).toBe(store);
    const random = options.random();
    expect(random).toBeGreaterThanOrEqual(0);
    expect(random).toBeLessThan(1);
    expect(options.view()).toBe('session.chat');
    expect(Math.abs(options.now() - Date.now())).toBeLessThan(1000);
    expect(options.observe(() => undefined)).toBeNull();
    const stop = startLongTaskMonitor(options);
    expect(win.setInterval).toHaveBeenCalledWith(expect.any(Function), TICK_MS);
    expect(win.setInterval).toHaveBeenCalledWith(expect.any(Function), CHECK_INTERVAL_MS);
    stop();
    expect(win.clearInterval).toHaveBeenCalledWith(7);
    win.dispatchEvent(new Event('pagehide'));
    expect(store.items.size).toBe(0);
  });

  it('runs without storage when the window refuses it', () => {
    const { win } = fakeWindow('throws');
    const options = browserMonitorOptions(win, { origin: HUB, version: '1.4.0', fetch: vi.fn() });
    expect(options.storage).toBeNull();
  });
});
