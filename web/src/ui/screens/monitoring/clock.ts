// One clock of whole seconds for the time cells of the monitoring screen ("Updated 5 s ago"): a single interval shared
// by every cell that shows an age, running only while one is mounted. Only the cells re-render each second, never the
// tree (ADR-086 section 1).
import { useSyncExternalStore } from 'react';

export const SECOND_MS = 1000;

const listeners = new Set<() => void>();
let now = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;

function tick(): void {
  now = Date.now();
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    // React reads the snapshot again after subscribing: a cell shown first with an older time corrects itself
    now = Date.now();
    timer = setInterval(tick, SECOND_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size > 0) return;
    clearInterval(timer!);
    timer = null;
  };
}

/** While no cell is shown the time is refreshed once it is a second off (stable between two reads of a render). */
function snapshot(): number {
  if (timer === null && Math.abs(Date.now() - now) >= SECOND_MS) now = Date.now();
  return now;
}

/** The current time, updated every second while the calling component is mounted. */
export function useSecond(): number {
  return useSyncExternalStore(subscribe, snapshot);
}
