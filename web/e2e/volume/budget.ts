// The budget every view of the web client must keep under the volume data set (ADR-086 §4; the reasons for each
// number: docs/platform/decisions/KOR-35.md) and `measure`, which measures one view, prints the values and records
// the measured ids for the run-time completeness check of the global teardown.
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { type CDPSession, expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { WEB_DIR } from '../fixtures/env.ts';
import { measuredFile } from './completeness.ts';
import { liveLoad, liveWritten } from './data.ts';
import { UNSEEDABLE } from './gaps.ts';
import { calibrationMs, factorOf, REFERENCE_MS, slowdown } from './speed.ts';

export const BUDGET = {
  /** Until the view's primary control is visible and enabled (desktop, phone with 4× CPU throttling). */
  usableMs: { desktop: 10_000, phone: 20_000 },
  /** DOM elements of the whole page. */
  maxElements: 6000,
  /** Used JS heap after a garbage collection. */
  maxHeapBytes: 300 * 1024 * 1024,
  /** Long-task time within a quiet window of `quietMs`. */
  quietMs: 2000,
  maxQuietLongTaskMs: 300,
  /** Longest frame gap while the page scrolls. */
  maxScrollGapMs: 500,
  /** Share of long-task time while 20 sessions in other tasks write 200 events per second; the page must receive at
   * least `minReceived` of the events written in the window over its live channel (else the load never reached it). */
  live: { sessions: 20, rate: 200, maxShare: 0.5, minReceived: 0.5 },
  /** Cost at 10 000 items per chat kind against 1 000 items per kind. */
  maxScalingFactor: 2,
  /** Time until usable with 10× the data: at most this factor, or at most `scalingSlackMs` more (sub-second times are
   *  dominated by noise of the shared test device; measured 2.0–2.5×, ADR-086 §4). */
  maxTimeScalingFactor: 3,
  scalingSlackMs: 1000,
} as const;

/** How long `measure` waits for the primary control at most: three times the budget (times the slowdown, at least
 * two), then the view counts as stuck. */
const WAIT_FACTOR = 3;

/**
 * Long tasks, frame gaps and the `event` frames of the live channel (`GET /v1/stream`, read with `fetch`) the page
 * received from its start (read by `measure`); add with `page.addInitScript`. The frames are counted on a tee of the
 * response body, so the app reads the stream unchanged.
 */
export function instrumentation(): void {
  const w = window as unknown as {
    __long: { start: number; duration: number }[];
    __gaps: number[];
    __events: number;
  };
  w.__long = [];
  w.__gaps = [];
  w.__events = 0;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const response = await nativeFetch(input, init);
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (!url.includes('/v1/stream') || response.body === null) return response;
    const [app, counted] = response.body.tee();
    void (async () => {
      const reader = counted.getReader();
      const decoder = new TextDecoder();
      let tail = '';
      for (;;) {
        const { value, done } = await reader.read().catch(() => ({ value: undefined, done: true }));
        if (done) return;
        const parts = (tail + decoder.decode(value, { stream: true })).split('event: event\n');
        w.__events += parts.length - 1;
        tail = parts[parts.length - 1]!.slice(-12);
      }
    })();
    Object.defineProperty(response, 'body', { value: app });
    return response;
  };
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) w.__long.push({ start: entry.startTime, duration: entry.duration });
  }).observe({ type: 'longtask', buffered: true });
  let last = performance.now();
  const frame = (now: number) => {
    w.__gaps.push(now - last);
    last = now;
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

interface Probe {
  readonly busy: number;
  readonly gap: number;
  readonly elements: number;
  readonly now: number;
  /** Live `event` frames received since the page started. */
  readonly events: number;
}

/** Long-task time since `since` (page clock), the longest frame gap since the last call, the element count. */
function probe(page: Page, since: number): Promise<Probe> {
  return page.evaluate((from) => {
    const w = window as unknown as {
      __long?: { start: number; duration: number }[];
      __gaps?: number[];
      __events?: number;
    };
    const busy = (w.__long ?? []).filter((task) => task.start >= from).reduce((sum, task) => sum + task.duration, 0);
    const gap = Math.max(0, ...(w.__gaps ?? []));
    w.__gaps = [];
    const elements = document.getElementsByTagName('*').length;
    return { busy, gap, elements, now: performance.now(), events: w.__events ?? 0 };
  }, since);
}

/** The CDP session a page already has (e.g. the one of its virtual authenticator): measurements reuse it, because
 * detaching a further session of the page also ended the virtual authenticator's ceremonies. */
const sessions = new WeakMap<Page, CDPSession>();
export function useCdp(page: Page, cdp: CDPSession): void {
  sessions.set(page, cdp);
}

async function cdpOf(page: Page): Promise<CDPSession> {
  let cdp = sessions.get(page);
  if (!cdp) {
    cdp = await page.context().newCDPSession(page);
    sessions.set(page, cdp);
  }
  return cdp;
}

/** Used JS heap after a garbage collection (CDP `Performance.getMetrics`, `JSHeapUsedSize`). */
async function heapBytes(page: Page): Promise<number> {
  const cdp = await cdpOf(page);
  await cdp.send('HeapProfiler.collectGarbage');
  await cdp.send('Performance.enable');
  const { metrics } = await cdp.send('Performance.getMetrics');
  return metrics.find((metric) => metric.name === 'JSHeapUsedSize')?.value ?? 0;
}

/** The page's scroll target: the given locator, else the document. */
async function scrollTop(page: Page, target: Locator | null): Promise<number> {
  return target
    ? target.evaluate((node) => (node as HTMLElement).scrollTop)
    : page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);
}

/** Whether the target (else the document) has content beyond its height. */
async function scrollable(page: Page, target: Locator | null): Promise<boolean> {
  const check = (node: Element | null) => (node ? node.scrollHeight > node.clientHeight + 1 : false);
  return target
    ? target.evaluate(check)
    : page.evaluate(() => {
        const node = document.scrollingElement;
        return node ? node.scrollHeight > node.clientHeight + 1 : false;
      });
}

/** Scrolls by wheel (over the target), down and, if the view starts at its end, up; waits after each input until the
 * position changed (a state, no fixed deadline: up to `waitMs`, which grows with the slowdown); the distance moved
 * and the longest frame gap meanwhile (null: nothing to scroll). */
async function scroll(
  page: Page,
  target: Locator | null,
  waitMs: number,
): Promise<{ moved: number; gap: number } | null> {
  if (!(await scrollable(page, target).catch(() => false))) return null;
  if (target) await target.hover({ timeout: waitMs }).catch(() => undefined);
  await probe(page, 0); // resets the frame gaps
  const before = await scrollTop(page, target);
  let moved = 0;
  for (const delta of [600, -600]) {
    for (let step = 0; step < 4; step += 1) {
      const at = await scrollTop(page, target);
      await page.mouse.wheel(0, delta);
      await expect
        .poll(() => scrollTop(page, target), { timeout: waitMs, intervals: [50, 100, 200] })
        .not.toBe(at)
        .catch(() => undefined);
      moved = Math.max(moved, Math.abs((await scrollTop(page, target)) - before));
    }
    if (moved > 0) break;
  }
  const { gap } = await probe(page, 0);
  return { moved, gap };
}

export interface Measurement {
  readonly project: string;
  readonly id: string;
  /** Until the primary control was usable (null: not within the wait). */
  readonly usableMs: number | null;
  readonly elements: number;
  readonly heapMb: number;
  readonly longTaskMs: number;
  /** Long-task share under live load (only with `live`). */
  readonly liveShare: number | null;
  /** Live events the page received in the live window, and the number written meanwhile (only with `live`). */
  readonly liveEvents: { readonly received: number; readonly written: number } | null;
  readonly scrolledPx: number | null;
  readonly scrollGapMs: number | null;
  /** Slowdown of the tab against the idle reference (the time budgets were multiplied by it). */
  readonly slowdown: number;
  /** The calibration work's time in the tab after the measurement (ms). */
  readonly calibrationMs: number;
  /** How often the window controls were used before the counts (earlier rows, next page). */
  readonly grown: number;
  /** The most rows any shared list shows after the moves. */
  readonly shownRows: number;
  readonly violations: string[];
}

export interface MeasureOptions {
  /** The view's primary control (visible and enabled). */
  readonly primary: Locator;
  /** Shown once the view's data is shown (e.g. its first row); usable means both. */
  readonly ready?: Locator;
  /** When the user acted (ms since epoch; default: now). */
  readonly since?: number;
  /** The scroll container (default: the document); null skips the scroll check (a view without scrolling content). */
  readonly scroll?: Locator | null;
  /** Measure the busy share under live load instead of the quiet window. */
  readonly live?: boolean;
  /** Further ids the same measurement covers (e.g. the chat kinds shown in the measured chat). */
  readonly also?: readonly string[];
}

/** What the budgets read of a test or worker. */
type Run = { readonly project: { readonly name: string } };

const phone = (info: Run): boolean => info.project.name.endsWith('-mobile');

/**
 * Measures the current view against the budget: waits for `primary`, moves the list window (`grow`), then counts DOM
 * elements, reads the heap,
 * watches the main thread for a quiet window (or the live-load window), scrolls; prints one `KOR-35 …` line, records
 * every id, and fails listing each exceeded bound.
 */
/** The window controls of the shared list (`ui/lists/WindowedList.tsx`: `<list>.older`, `<list>.load-more` and the
 * lists' own names for them): earlier rows and the next server page. */
const GROW = [
  'button[data-testid$=".older"]',
  'button[data-testid$=".load-more"]',
  'button[data-testid="chat.earlier"]',
  'button[data-testid="tasks.more"]',
  'button[data-testid="notifications.more"]',
  'button[data-testid="operations.more"]',
  'button[data-testid="audit.more"]',
].join(', ');
/** The largest window of the shared list (`maxRows` of every `WindowedList`, ADR-086 §1): no list shows more rows. */
export const MAX_SHOWN = 200;
/** Upper bound of the steps after "usable" (window moves, quiet windows, heap, scrolling) on an answering page. */
const SAFETY_MS = 120_000;

/** Whether the page runs a trivial script within `ms` (a frozen main thread does not). */
async function answers(page: Page, ms: number): Promise<boolean> {
  return Promise.race([
    page.evaluate(() => true).catch(() => false),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), ms)),
  ]);
}

/** Quiet windows `measure` waits for at most (each `BUDGET.quietMs`). */
const QUIET_TRIES = 3;
/** After this long without an answer (times the slowdown) the page counts as frozen. */
const FROZEN_MS = 20_000;
/** How often `measure` uses the window controls before it counts: with pages of 50 or more rows, five moves hold
 * more rows than the largest window (`MAX_SHOWN`), so a list that renders all it holds exceeds the row bound. */
const GROW_STEPS = 5;

/**
 * Uses the window controls of the view up to `GROW_STEPS` times (each until the page changed): the view then holds
 * many more rows than it shows, and a list that is not windowed shows them all (the element bound fails). Returns the
 * number of uses.
 */
async function grow(page: Page, waitMs: number, moves: string[] = []): Promise<number> {
  let used = 0;
  for (let step = 0; step < GROW_STEPS; step += 1) {
    const control = page.locator(`${GROW}:not([disabled])`).first();
    if (!(await control.isVisible().catch(() => false))) break;
    const before = await page.evaluate(() => document.body.innerHTML.length);
    // the control may be replaced meanwhile (a page that is still loading re-renders it): every call is bounded, a
    // vanished control ends the moves instead of waiting until the safety bound (which reads as a frozen page)
    const name = await control.getAttribute('data-testid', { timeout: waitMs }).catch(() => null);
    if (name === null) break;
    const clicked = await control.click({ timeout: waitMs }).then(
      () => true,
      () => false,
    );
    if (!clicked) break;
    moves.push(name);
    used += 1;
    await expect
      .poll(() => page.evaluate(() => document.body.innerHTML.length), { timeout: waitMs, intervals: [100, 200, 400] })
      .not.toBe(before)
      .catch(() => undefined);
  }
  return used;
}

const pending = new WeakMap<Page, number>();
/** Pages that froze during a measurement (`fixtures.ts` replaces them). */
export const frozenPages = new WeakSet<Page>();

/** The CPU throttling a project applies on purpose (its budgets include it). */
const throttle = (info: Run): number => (phone(info) ? 4 : 1);

/**
 * The CPU throttling applied to the measured tab: the project's own (phone 4×, part of its reference), or – for the
 * stability proof of the gate (ADR-086 §4) – `VOLUME_STRESS_CPU` (e.g. 6), which the calibration sees as a slowdown
 * and the time budgets follow, while element and heap bounds stay.
 */
export function appliedThrottle(info: Run, env: NodeJS.ProcessEnv = process.env): number {
  const stress = Number(env.VOLUME_STRESS_CPU ?? 0);
  return Math.max(throttle(info), Number.isFinite(stress) ? stress : 0);
}

/** Calibrates the tab before the user action a measurement starts with (`measure` takes the larger of this and its
 * own calibration after the measurement). */
export async function calibrate(page: Page, info: Run): Promise<void> {
  pending.set(page, await slowdown(page, throttle(info)));
}

/**
 * Measures the current view against the budget: waits for `primary`, moves the list window (`grow`), then counts DOM
 * elements, reads the heap,
 * watches the main thread for a quiet window (or the live-load window), scrolls; prints one `KOR-35 …` line, records
 * every id, and fails listing each exceeded bound. Time budgets are scaled by the slowdown of the tab calibrated in
 * the run (`speed.ts`); element and heap bounds are not.
 */
export async function measure(page: Page, info: TestInfo, id: string, options: MeasureOptions): Promise<Measurement> {
  const since = options.since ?? Date.now();
  const before = pending.get(page) ?? 1;
  pending.delete(page);
  const budgetMs = phone(info) ? BUDGET.usableMs.phone : BUDGET.usableMs.desktop;
  let usableMs: number | null;
  const waitMs = budgetMs * WAIT_FACTOR * Math.max(before, 2);
  const left = () => ({ timeout: Math.max(1000, since + waitMs - Date.now()) });
  try {
    if (options.ready) await expect(options.ready).toBeVisible(left());
    await expect(options.primary).toBeVisible(left());
    await expect(options.primary).toBeEnabled(left());
    usableMs = Date.now() - since;
  } catch {
    usableMs = null;
  }
  const moves: string[] = [];
  // everything after "usable" talks to the page; a page whose main thread never answers again is frozen – the very
  // failure the gate exists for – and is reported as such instead of hanging the test
  const steps = async () => {
    const grown = usableMs === null ? 0 : await grow(page, 2000 * Math.max(before, 2), moves);
    if (!(await answers(page, FROZEN_MS * Math.max(before, 1)))) return null;
    let liveShare: number | null = null;
    let liveEvents: Measurement['liveEvents'] = null;
    let longTaskMs: number;
    if (options.live) {
      await liveLoad(BUDGET.quietMs / 1000 + 2, BUDGET.live.rate);
      await page.waitForTimeout(500);
      const start = await probe(page, 0);
      const writtenBefore = await liveWritten();
      await page.waitForTimeout(BUDGET.quietMs);
      const writtenAfter = await liveWritten();
      // frames still on their way when the window closes arrive within a moment: counted after a short grace
      await page.waitForTimeout(500);
      const end = await probe(page, start.now);
      longTaskMs = end.busy;
      liveShare = end.busy / (end.now - start.now);
      liveEvents = { received: end.events - start.events, written: writtenAfter - writtenBefore };
    } else {
      // the view must become quiet: the least busy of up to `QUIET_TRIES` consecutive windows counts (work that a
      // view finishes after it is shown, or a busy device, end; a view that keeps computing does not)
      longTaskMs = Infinity;
      for (let attempt = 0; attempt < QUIET_TRIES && longTaskMs > BUDGET.maxQuietLongTaskMs; attempt += 1) {
        const start = await probe(page, 0);
        await page.waitForTimeout(BUDGET.quietMs);
        longTaskMs = Math.min(longTaskMs, (await probe(page, start.now)).busy);
      }
    }
    const elements = (await probe(page, 0)).elements;
    const shownRows = await page
      .locator('[data-shown]')
      .evaluateAll((nodes) => Math.max(0, ...nodes.map((node) => Number((node as HTMLElement).dataset.shown) || 0)));
    const heap = await heapBytes(page);
    // the view is still there after the window moves (a blank or crashed page has few elements, too)
    const kept = usableMs === null || (await options.primary.isVisible());
    const scrolled =
      options.scroll === null ? null : await scroll(page, options.scroll ?? null, 2000 * Math.max(before, 2));
    return { grown, liveShare, liveEvents, longTaskMs, elements, shownRows, heap, kept, scrolled };
  };
  const frozenMs = FROZEN_MS * Math.max(before, 1);
  // the page must answer a trivial script within `frozenMs` after the window moves; the rest is bounded by SAFETY_MS
  const done = await Promise.race([
    steps(),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), SAFETY_MS * Math.max(before, 1))),
  ]);
  const { grown, liveShare, liveEvents, longTaskMs, elements, shownRows, heap, kept, scrolled } = done ?? {
    grown: moves.length,
    liveShare: null,
    liveEvents: null,
    longTaskMs: frozenMs,
    elements: -1,
    shownRows: 0,
    heap: 0,
    kept: false,
    scrolled: null,
  };
  if (!done) {
    // end the frozen renderer, so that the next view can load (the tab and its CDP session stay)
    // the tab is replaced before the next view (`fixtures.ts`): a frozen renderer cannot navigate
    frozenPages.add(page);
  }
  const calibration = done ? await calibrationMs(page) : REFERENCE_MS * throttle(info); // a frozen tab cannot be calibrated: no allowance for it
  const factor = Math.max(before, factorOf(calibration, throttle(info)));
  const usableBudget = Math.round(budgetMs * factor);
  const quietBudget = Math.round(BUDGET.maxQuietLongTaskMs * factor);
  const gapBudget = Math.round(BUDGET.maxScrollGapMs * factor);
  const shareBudget = Math.min(0.9, BUDGET.live.maxShare * factor);
  const violations: string[] = [];
  if (!done) violations.push(`page frozen: no answer within ${Math.round(frozenMs / 1000)} s after it was usable`);
  else if (!kept) violations.push('the view is gone after the window controls were used');
  if (usableMs === null || usableMs > usableBudget)
    violations.push(`usable ${usableMs === null ? `not within ${Math.round(waitMs)}` : usableMs} ms > ${usableBudget}`);
  if (elements >= BUDGET.maxElements) violations.push(`${elements} elements ≥ ${BUDGET.maxElements}`);
  if (shownRows > MAX_SHOWN) violations.push(`a list shows ${shownRows} rows > ${MAX_SHOWN} (not windowed)`);
  if (heap >= BUDGET.maxHeapBytes) violations.push(`heap ${mb(heap)} MB ≥ ${mb(BUDGET.maxHeapBytes)}`);
  if (liveShare !== null && liveShare >= shareBudget)
    violations.push(`long tasks ${pct(liveShare)} % of the live window ≥ ${pct(shareBudget)}`);
  // a quiet page under no load would pass the share bound: the load must have reached the page
  if (liveEvents !== null && liveEvents.received < BUDGET.live.minReceived * liveEvents.written)
    violations.push(`the page received ${liveEvents.received} live events of ${liveEvents.written} written`);
  if (liveShare === null && longTaskMs > quietBudget)
    violations.push(`long tasks ${Math.round(longTaskMs)} ms in ${BUDGET.quietMs} ms > ${quietBudget}`);
  if (scrolled && scrolled.moved === 0) violations.push('scrolling did not move the page');
  if (scrolled && scrolled.gap >= gapBudget)
    violations.push(`frame gap ${Math.round(scrolled.gap)} ms while scrolling ≥ ${gapBudget}`);
  const result: Measurement = {
    project: info.project.name,
    id,
    usableMs,
    elements,
    heapMb: mb(heap),
    longTaskMs: Math.round(longTaskMs),
    liveShare: liveShare === null ? null : pct(liveShare),
    liveEvents,
    scrolledPx: scrolled ? Math.round(scrolled.moved) : null,
    scrollGapMs: scrolled ? Math.round(scrolled.gap) : null,
    slowdown: Math.round(factor * 100) / 100,
    calibrationMs: Math.round(calibration),
    grown,
    shownRows,
    violations,
  };
  // the further ids count only if the page shows them (a list with shown rows, the active tab)
  const shown: string[] = [];
  for (const other of options.also ?? []) {
    if (other in UNSEEDABLE) continue; // not in the data set today (gaps.ts), reported by the teardown
    if (done && (await isShown(page, other))) shown.push(other);
    else violations.push(`${other} not shown`);
  }
  const used = moves.length ? ` [${moves.join(', ')}]` : '';
  console.log(`KOR-35 ${result.project} ${id}: ${describe(result)}${used}`);
  record(result, shown);
  // soft: a test that measures several views goes on and measures (and records) each of them
  expect.soft(violations, `KOR-35 budget of ${id}`).toEqual([]);
  return result;
}

/**
 * Whether the page shows what an id names: `list:<id>` a shared list `[data-list=<id>]` with rows shown
 * (`data-shown` > 0); `tab:<id>` the active task tab; `session:<tab>` the active session tab; other ids (views opened
 * by their route) need no further check.
 */
export async function isShown(page: Page, id: string): Promise<boolean> {
  const [kind, name] = [id.slice(0, id.indexOf(':')), id.slice(id.indexOf(':') + 1)];
  if (kind === 'list')
    return page
      .locator(`[data-list="${name}"] [data-shown]`)
      .evaluateAll((nodes) => nodes.some((node) => Number((node as HTMLElement).dataset.shown) > 0));
  if (kind === 'tab' && name !== 'chat')
    return (await page.locator(`[data-testid="task.tabs.${name}"][aria-current="page"]`).count()) > 0;
  if (kind === 'session' && name !== 'terminal')
    return (await page.locator(`[data-testid="session.tabs.${name}"][aria-current="page"]`).count()) > 0;
  if (kind === 'tab' || kind === 'session')
    // the default tab: shown without `aria-current` on some layouts – its content must be there
    return (await page.getByTestId(name === 'chat' ? 'chat.timeline' : 'terminal').count()) > 0;
  return true;
}

const mb = (bytes: number): number => Math.round(bytes / 1024 / 1024);
const pct = (share: number): number => Math.round(share * 100);

export function describe(result: Measurement): string {
  const parts = [
    `usable ${result.usableMs === null ? 'stuck' : `${result.usableMs} ms`}`,
    `elements ${result.elements} after ${result.grown} window moves (max ${result.shownRows} rows shown)`,
    `heap ${result.heapMb} MB`,
    result.liveShare === null
      ? `long tasks ${result.longTaskMs} ms/2 s`
      : `live long tasks ${result.liveShare} % (${result.liveEvents?.received}/${result.liveEvents?.written} events)`,
    result.scrolledPx === null ? 'no scroll' : `scrolled ${result.scrolledPx} px, frame gap ${result.scrollGapMs} ms`,
    `slowdown ${result.slowdown} (calibration ${result.calibrationMs} ms)`,
  ];
  return `${parts.join(', ')}${result.violations.length ? ` – over budget: ${result.violations.join('; ')}` : ''}`;
}

/** Appends the measurement (and the ids it also covers) to the run's results file. */
export function record(result: Measurement, also: readonly string[] = []): void {
  const file = measuredFile(WEB_DIR);
  mkdirSync(dirname(file), { recursive: true });
  const lines = [result, ...also.map((id) => ({ ...result, id, of: result.id }))].map((item) => JSON.stringify(item));
  appendFileSync(file, `${lines.join('\n')}\n`);
}
