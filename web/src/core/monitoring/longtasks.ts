// Early warning in operation (ADR-086 section 5): the web client counts long tasks of the main thread per view type
// and reports the counters anonymously to the hub.
// - Source: `PerformanceObserver` with entry type `longtask` where the browser supports it; otherwise a 1 s timer whose
//   tick arriving more than `THRESHOLD_MS` late counts as a block of that lateness. Ticks while the page is hidden
//   (browsers throttle timers there) and gaps over `MAX_GAP_MS` (a suspended device) are not blocks.
// - Counted per view type (`viewOf`, a closed list, never an ID or a path) are only blocks over `THRESHOLD_MS`:
//   `count`, `blocked_ms` (sum of the durations, rounded) and `max_bucket` – the bucket of the longest block: the index
//   i of `BUCKETS_MS` (200, 500, 1000, 2000, 5000, 10000, 30000 ms; `buckets_ms` of contracts/enums/ui-views.json) with
//   BUCKETS_MS[i] <= duration < BUCKETS_MS[i+1], the last bucket open. Counters are bounded by the hub's clamps
//   (`MAX_COUNT`, `MAX_BLOCKED_MS`, `limits` of the contract).
// - Sending: at most once per `SEND_INTERVAL_MS` per browser (checked every `CHECK_INTERVAL_MS` and when the page is
//   hidden) and only when something was counted, `POST /v1/telemetry/ui` with `{version, views:{<view>:{count,
//   blocked_ms, max_bucket}}}` – plain `fetch`, no signature, no cookies (`credentials: 'omit'`), no referrer
//   (`referrerPolicy: 'no-referrer'`), `keepalive` so that a report sent while the page goes away still leaves. A failed
//   report puts its counters back for the next attempt. The first report of a page is due at a random time within the
//   hour after it loaded (`random`), so reports do not reveal when a page was opened and do not arrive in bursts.
// - Shared by the pages of a browser through `localStorage` (best effort): the time of the last report (every page
//   honours it) and a pool of counters not yet reported. A page puts its counters into the pool when it is hidden; the
//   page that sends takes the pool with its own counters and empties it. A counter is therefore either in one page or
//   in the pool and is reported once, however many tabs are open or reloaded.
import type { PrefStorage } from '../account/prefs';
import { isRecord } from '../registry';
import { isViewName, type ViewName, viewOf } from './views';

export const THRESHOLD_MS = 200;
export const BUCKETS_MS = [200, 500, 1000, 2000, 5000, 10000, 30000] as const;
export const TICK_MS = 1000;
export const MAX_GAP_MS = 60_000;
export const SEND_INTERVAL_MS = 3_600_000;
export const CHECK_INTERVAL_MS = 60_000;
export const MAX_COUNT = 100_000;
export const MAX_BLOCKED_MS = 3_600_000;
export const TELEMETRY_PATH = '/v1/telemetry/ui';
export const STORAGE_KEY = 'agent-platform.ui-health';

export interface ViewCounts {
  readonly count: number;
  readonly blocked_ms: number;
  readonly max_bucket: number;
}

export type Counts = Partial<Record<ViewName, ViewCounts>>;

/** The bucket (index into `BUCKETS_MS`) of a block of `ms`; the first bucket for anything shorter. */
export const bucketOf = (ms: number): number =>
  BUCKETS_MS.reduce<number>((best, bound, index) => (ms >= bound ? index : best), 0);

/** Adds counters of one view to `counts`, bounded. */
export function addCounts(counts: Counts, view: ViewName, add: ViewCounts): void {
  const had = counts[view] ?? { count: 0, blocked_ms: 0, max_bucket: 0 };
  counts[view] = {
    count: Math.min(MAX_COUNT, had.count + add.count),
    blocked_ms: Math.min(MAX_BLOCKED_MS, had.blocked_ms + add.blocked_ms),
    max_bucket: Math.max(had.max_bucket, add.max_bucket),
  };
}

/** Adds one block of `ms` to `view` when it is longer than `THRESHOLD_MS`. */
export function addBlock(counts: Counts, view: ViewName, ms: number): void {
  if (ms > THRESHOLD_MS) addCounts(counts, view, { count: 1, blocked_ms: Math.round(ms), max_bucket: bucketOf(ms) });
}

/** A source of long tasks: calls `onBlock` with each duration and returns its stop function, or null if unsupported. */
export type LongTaskSource = (onBlock: (durationMs: number) => void) => (() => void) | null;

/** The part of `PerformanceObserver` the monitor uses. */
export interface ObserverClass {
  readonly supportedEntryTypes?: readonly string[];
  new (callback: (list: { getEntries(): readonly { readonly duration: number }[] }) => void): {
    observe(options: { type: string; buffered?: boolean }): void;
    disconnect(): void;
  };
}

/** `LongTaskSource` on a `PerformanceObserver` class; null when it is missing or lacks the `longtask` entry type. */
export function longTaskObserver(Observer: ObserverClass | undefined): LongTaskSource {
  return (onBlock) => {
    if (Observer?.supportedEntryTypes?.includes('longtask') !== true) return null;
    const observer = new Observer((list) => list.getEntries().forEach((entry) => onBlock(entry.duration)));
    observer.observe({ type: 'longtask', buffered: true });
    return () => observer.disconnect();
  };
}

export interface LongTaskMonitorOptions {
  /** Origin of the hub (`config.origin`). */
  readonly origin: string;
  /** Version of the platform build the client belongs to (`config.version`). */
  readonly version: string;
  readonly fetch: typeof globalThis.fetch;
  /** The current view type. */
  readonly view: () => ViewName;
  readonly observe: LongTaskSource;
  /** Wall clock in ms (the last report time is kept across reloads). */
  readonly now: () => number;
  readonly setInterval: (fn: () => void, ms: number) => unknown;
  readonly clearInterval: (handle: unknown) => void;
  /** Target of `pagehide` (the window). */
  readonly page: EventTarget;
  /** Target of `visibilitychange` (the document). */
  readonly document: EventTarget & { readonly visibilityState: string };
  readonly storage: PrefStorage | null;
  /** A number in [0, 1) (`Math.random`): the delay of the first report within the hour. */
  readonly random: () => number;
}

interface Saved {
  readonly sent: number;
  readonly views: Counts;
}

const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

const isViewCounts = (value: unknown): value is ViewCounts =>
  isRecord(value) &&
  isCount(value.count) &&
  isCount(value.blocked_ms) &&
  isCount(value.max_bucket) &&
  value.max_bucket < BUCKETS_MS.length;

/** The stored state, checked; anything unreadable or malformed is dropped (a time in the future as well). */
export function loadSaved(storage: PrefStorage | null, now: number): Saved {
  let parsed: unknown = null;
  try {
    parsed = JSON.parse(storage?.getItem(STORAGE_KEY) ?? 'null');
  } catch {
    // an unreadable storage or entry behaves as empty
  }
  const record = isRecord(parsed) ? parsed : {};
  const sent = isCount(record.sent) && record.sent <= now ? record.sent : 0;
  const views: Counts = {};
  for (const [view, counts] of Object.entries(isRecord(record.views) ? record.views : {})) {
    if (isViewName(view) && isViewCounts(counts)) addCounts(views, view, counts);
  }
  return { sent, views };
}

/** Adds every counter of `from` to `into`; returns `into`. */
function merge(into: Counts, from: Counts): Counts {
  for (const [view, counts] of Object.entries(from) as [ViewName, ViewCounts][]) addCounts(into, view, counts);
  return into;
}

/** Whether `saved` was written (no storage or a refusing one: not). */
function save(storage: PrefStorage | null, saved: Saved): boolean {
  try {
    storage!.setItem(STORAGE_KEY, JSON.stringify(saved));
    return true;
  } catch {
    // no storage or a refusing one: the state stays in this page
    return false;
  }
}

/** Starts the monitor; returns its stop function. */
export function startLongTaskMonitor(options: LongTaskMonitorOptions): () => void {
  const { now, storage, document: doc } = options;
  // the time of the last report seen by this page (the pool in storage has the one of the whole browser)
  let sent = 0;
  let pending: Counts = {};
  const first = now() + options.random() * SEND_INTERVAL_MS;
  const record = (ms: number) => addBlock(pending, options.view(), ms);

  const restore = (views: Counts) => merge(pending, views);
  const send = () => {
    const at = now();
    const shared = loadSaved(storage, at);
    sent = Math.max(sent, shared.sent);
    const counted = Object.keys(shared.views).length + Object.keys(pending).length > 0;
    if (!counted || at < Math.max(sent + SEND_INTERVAL_MS, first)) return;
    sent = at;
    // the report carries this page's counters and the pool of the browser – the pool only when it could be emptied
    const views = merge(save(storage, { sent, views: {} }) ? shared.views : {}, pending);
    pending = {};
    if (Object.keys(views).length === 0) return;
    options
      .fetch(`${options.origin}${TELEMETRY_PATH}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ version: options.version, views }),
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
        cache: 'no-store',
        keepalive: true,
      })
      .then(
        (response) => {
          if (!response.ok) restore(views);
        },
        () => restore(views),
      );
  };

  let last = now();
  const stopObserver = options.observe(record);
  const tick = () => {
    const at = now();
    const late = at - last - TICK_MS;
    last = at;
    if (doc.visibilityState === 'visible' && late <= MAX_GAP_MS) record(late);
  };
  const ticker = stopObserver === null ? options.setInterval(tick, TICK_MS) : null;
  const checker = options.setInterval(send, CHECK_INTERVAL_MS);

  // a hidden page may never come back: its counters go into the pool for whichever page reports next
  const hide = () => {
    send();
    const shared = loadSaved(storage, now());
    if (save(storage, { sent: Math.max(sent, shared.sent), views: merge(shared.views, pending) })) pending = {};
  };
  const visibility = () => {
    last = now();
    if (doc.visibilityState === 'hidden') hide();
  };
  doc.addEventListener('visibilitychange', visibility);
  options.page.addEventListener('pagehide', hide);

  return () => {
    if (stopObserver === null) options.clearInterval(ticker);
    else stopObserver();
    options.clearInterval(checker);
    doc.removeEventListener('visibilitychange', visibility);
    options.page.removeEventListener('pagehide', hide);
  };
}

/** The parts of the browser window the monitor uses. */
export interface MonitorWindow extends EventTarget {
  readonly location: { readonly pathname: string; readonly search: string };
  readonly document: EventTarget & { readonly visibilityState: string };
  readonly PerformanceObserver?: ObserverClass;
  readonly localStorage: PrefStorage;
  setInterval(fn: () => void, ms: number): number;
  clearInterval(handle: number): void;
}

/** The options for the page's own window: view from `location`, the window's observer, timers and storage. */
export function browserMonitorOptions(
  win: MonitorWindow,
  hub: { readonly origin: string; readonly version: string; readonly fetch: typeof globalThis.fetch },
): LongTaskMonitorOptions {
  let storage: PrefStorage | null = null;
  try {
    storage = win.localStorage;
  } catch {
    // no storage (private mode, policy): the limit and the counters live in memory only
  }
  return {
    ...hub,
    view: () => viewOf(win.location.pathname, win.location.search),
    observe: longTaskObserver(win.PerformanceObserver),
    now: () => Date.now(),
    setInterval: (fn, ms) => win.setInterval(fn, ms),
    clearInterval: (handle) => win.clearInterval(handle as number),
    page: win,
    document: win.document,
    storage,
    random: () => Math.random(),
  };
}
