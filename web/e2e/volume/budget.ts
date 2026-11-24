// The budget every view of the web client must keep under the volume data set (ADR-063 §4; the reasons for each
// number: docs/platform/decisions/KOR-35.md) and `measure`, which measures one view, prints the values and records
// the measured ids for the run-time completeness check of the global teardown.
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { WEB_DIR } from '../fixtures/env.ts';
import { measuredFile } from './completeness.ts';
import { liveLoad } from './data.ts';

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
  /** Share of long-task time while 20 sessions in other tasks write 200 events per second. */
  live: { sessions: 20, rate: 200, maxShare: 0.5 },
  /** Cost at 10 000 items per chat kind against 1 000 items per kind. */
  maxScalingFactor: 2,
} as const;

/** How long `measure` waits for the primary control at most: three times the budget, then the view counts as stuck. */
const WAIT_FACTOR = 3;

/** Long tasks and frame gaps of the page from its start (read by `measure`); add with `page.addInitScript`. */
export function instrumentation(): void {
  const w = window as unknown as { __long: { start: number; duration: number }[]; __gaps: number[] };
  w.__long = [];
  w.__gaps = [];
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
}

/** Long-task time since `since` (page clock), the longest frame gap since the last call, the element count. */
function probe(page: Page, since: number): Promise<Probe> {
  return page.evaluate((from) => {
    const w = window as unknown as { __long?: { start: number; duration: number }[]; __gaps?: number[] };
    const busy = (w.__long ?? []).filter((task) => task.start >= from).reduce((sum, task) => sum + task.duration, 0);
    const gap = Math.max(0, ...(w.__gaps ?? []));
    w.__gaps = [];
    return { busy, gap, elements: document.getElementsByTagName('*').length, now: performance.now() };
  }, since);
}

/** Used JS heap after a garbage collection (CDP `Performance.getMetrics`, `JSHeapUsedSize`). */
async function heapBytes(page: Page): Promise<number> {
  const cdp = await page.context().newCDPSession(page);
  try {
    await cdp.send('HeapProfiler.collectGarbage');
    await cdp.send('Performance.enable');
    const { metrics } = await cdp.send('Performance.getMetrics');
    return metrics.find((metric) => metric.name === 'JSHeapUsedSize')?.value ?? 0;
  } finally {
    await cdp.detach();
  }
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

/** Scrolls down by wheel (over the target), then up; the distance moved and the longest frame gap meanwhile (null:
 * nothing to scroll). */
async function scroll(page: Page, target: Locator | null): Promise<{ moved: number; gap: number } | null> {
  if (!(await scrollable(page, target).catch(() => false))) return null;
  if (target) await target.hover({ timeout: 5000 }).catch(() => undefined);
  await probe(page, 0); // resets the frame gaps
  const before = await scrollTop(page, target);
  let moved = 0;
  for (let step = 0; step < 6; step += 1) {
    await page.mouse.wheel(0, 600);
    await page.waitForTimeout(150);
    moved = Math.max(moved, Math.abs((await scrollTop(page, target)) - before));
  }
  if (moved === 0) {
    // already at the end (a chat starts there): scroll up instead
    for (let step = 0; step < 6; step += 1) {
      await page.mouse.wheel(0, -600);
      await page.waitForTimeout(150);
      moved = Math.max(moved, Math.abs((await scrollTop(page, target)) - before));
    }
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
  readonly scrolledPx: number | null;
  readonly scrollGapMs: number | null;
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

const phone = (info: TestInfo): boolean => info.project.name.endsWith('-mobile');

/**
 * Measures the current view against the budget: waits for `primary`, then counts DOM elements, reads the heap,
 * watches the main thread for a quiet window (or the live-load window), scrolls; prints one `KOR-35 …` line, records
 * every id, and fails listing each exceeded bound.
 */
export async function measure(page: Page, info: TestInfo, id: string, options: MeasureOptions): Promise<Measurement> {
  const since = options.since ?? Date.now();
  const budgetMs = phone(info) ? BUDGET.usableMs.phone : BUDGET.usableMs.desktop;
  let usableMs: number | null;
  const left = () => ({ timeout: Math.max(1000, since + budgetMs * WAIT_FACTOR - Date.now()) });
  try {
    if (options.ready) await expect(options.ready).toBeVisible(left());
    await expect(options.primary).toBeVisible(left());
    await expect(options.primary).toBeEnabled(left());
    usableMs = Date.now() - since;
  } catch {
    usableMs = null;
  }
  let liveShare: number | null = null;
  let longTaskMs: number;
  if (options.live) {
    await liveLoad(BUDGET.quietMs / 1000 + 2, BUDGET.live.rate);
    await page.waitForTimeout(500);
    const start = await probe(page, 0);
    await page.waitForTimeout(BUDGET.quietMs);
    const end = await probe(page, start.now);
    longTaskMs = end.busy;
    liveShare = end.busy / (end.now - start.now);
  } else {
    const start = await probe(page, 0);
    await page.waitForTimeout(BUDGET.quietMs);
    longTaskMs = (await probe(page, start.now)).busy;
  }
  const elements = (await probe(page, 0)).elements;
  const heap = await heapBytes(page);
  const scrolled = options.scroll === null ? null : await scroll(page, options.scroll ?? null);
  const violations: string[] = [];
  if (usableMs === null || usableMs > budgetMs)
    violations.push(`usable ${usableMs === null ? `not within ${budgetMs * WAIT_FACTOR}` : usableMs} ms > ${budgetMs}`);
  if (elements >= BUDGET.maxElements) violations.push(`${elements} elements ≥ ${BUDGET.maxElements}`);
  if (heap >= BUDGET.maxHeapBytes) violations.push(`heap ${mb(heap)} MB ≥ ${mb(BUDGET.maxHeapBytes)}`);
  if (liveShare !== null && liveShare >= BUDGET.live.maxShare)
    violations.push(`long tasks ${pct(liveShare)} % of the live window ≥ ${pct(BUDGET.live.maxShare)}`);
  if (liveShare === null && longTaskMs > BUDGET.maxQuietLongTaskMs)
    violations.push(`long tasks ${Math.round(longTaskMs)} ms in ${BUDGET.quietMs} ms > ${BUDGET.maxQuietLongTaskMs}`);
  if (scrolled && scrolled.moved === 0) violations.push('scrolling did not move the page');
  if (scrolled && scrolled.gap >= BUDGET.maxScrollGapMs)
    violations.push(`frame gap ${Math.round(scrolled.gap)} ms while scrolling ≥ ${BUDGET.maxScrollGapMs}`);
  const result: Measurement = {
    project: info.project.name,
    id,
    usableMs,
    elements,
    heapMb: mb(heap),
    longTaskMs: Math.round(longTaskMs),
    liveShare: liveShare === null ? null : pct(liveShare),
    scrolledPx: scrolled ? Math.round(scrolled.moved) : null,
    scrollGapMs: scrolled ? Math.round(scrolled.gap) : null,
    violations,
  };
  console.log(`KOR-35 ${result.project} ${id}: ${describe(result)}`);
  record(result, options.also ?? []);
  // soft: a test that measures several views goes on and measures (and records) each of them
  expect.soft(violations, `KOR-35 budget of ${id}`).toEqual([]);
  return result;
}

const mb = (bytes: number): number => Math.round(bytes / 1024 / 1024);
const pct = (share: number): number => Math.round(share * 100);

export function describe(result: Measurement): string {
  const parts = [
    `usable ${result.usableMs === null ? 'stuck' : `${result.usableMs} ms`}`,
    `elements ${result.elements}`,
    `heap ${result.heapMb} MB`,
    result.liveShare === null ? `long tasks ${result.longTaskMs} ms/2 s` : `live long tasks ${result.liveShare} %`,
    result.scrolledPx === null ? 'no scroll' : `scrolled ${result.scrolledPx} px, frame gap ${result.scrollGapMs} ms`,
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
