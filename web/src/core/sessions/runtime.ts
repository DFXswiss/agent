// The runtime of a session or a task (concept section 7: runtime visible; KOR-40): from its start to its end, or to
// `now` while it runs; null when a time is malformed or the end of an ended object is unknown.
import { parseTime } from '../encoding/time';

/** Whole seconds from `start` to `end` (to `now` when `end` is null and the object still runs). */
export function runtimeSeconds(
  start: string,
  end: string | null | undefined,
  running: boolean,
  now: number,
): number | null {
  if (end == null && !running) return null;
  try {
    const from = parseTime(start);
    const to = end == null ? now : parseTime(end);
    return Math.max(0, Math.floor((to - from) / 1000));
  } catch {
    return null;
  }
}
