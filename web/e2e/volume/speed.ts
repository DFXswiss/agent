// Machine speed calibrated in the run (ADR-086 §4, decisions/KOR-35.md): the time budgets of the volume checks hold on
// an idle test device; on a busy one (parallel gates, other suites) the same page needs longer for the same work. A
// fixed piece of main-thread work (script, JSON, layout of a detached-free DOM fragment) is timed in the measured tab
// right before and after a measurement; its ratio to the reference time of the idle device is the slowdown the time
// budgets are scaled by. Structural bounds (DOM elements, heap, rows rendered) are never scaled: a real regression
// (e.g. a list that renders everything) breaks them on any machine.
import type { Page } from '@playwright/test';

/** Median time of the calibration work on the idle test device (Desktop Chrome, no throttling; measured 16–21 ms in
 * the runs of 2026-11-24), in ms. */
export const REFERENCE_MS = 20;
/** A slowdown beyond this makes time measurements meaningless: the budgets stop growing (the check then fails). */
export const MAX_SLOWDOWN = 8;

/** The calibration work in the page; returns its median duration over `runs` (ms on the page clock). */
function work(runs: number): number {
  const times: number[] = [];
  for (let run = 0; run < runs; run += 1) {
    const start = performance.now();
    let hash = 0;
    for (let index = 0; index < 1_500_000; index += 1) hash = (hash * 31 + index) | 0;
    const text = JSON.stringify(Array.from({ length: 2000 }, (_, index) => ({ index, hash, name: `row ${index}` })));
    const host = document.createElement('div');
    host.style.cssText = 'position:absolute;left:-10000px;top:0;width:400px';
    for (let index = 0; index < 300; index += 1) {
      const row = document.createElement('p');
      row.textContent = text.slice(index * 10, index * 10 + 80);
      host.append(row);
    }
    document.body.append(host);
    void host.getBoundingClientRect().height;
    host.remove();
    times.push(performance.now() - start);
  }
  times.sort((a, b) => a - b);
  return times[Math.floor(times.length / 2)]!;
}

/**
 * The slowdown of the tab now against the idle reference (≥ 1, ≤ `MAX_SLOWDOWN`). `throttle` is the CPU throttling the
 * project applies on purpose (the phone's 4×): its budgets already include it.
 */
export async function slowdown(page: Page, throttle = 1): Promise<number> {
  return factorOf(await calibrationMs(page), throttle);
}

/** The median time of the calibration work in the tab now (ms). */
export function calibrationMs(page: Page): Promise<number> {
  return page.evaluate(work, 3);
}

export const factorOf = (ms: number, throttle = 1): number =>
  Math.min(MAX_SLOWDOWN, Math.max(1, ms / (REFERENCE_MS * throttle)));
