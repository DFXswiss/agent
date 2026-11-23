// Runs of file changes and the cheap item comparison of the chat (06-web.md section 5.2, HOT-05). A checkout, a
// formatter or `npm install` reports one `session.file` per file – thousands in a row. Consecutive `session.file`
// items of one session become one `file-group` item ("483 files changed"); its body lists the file items in order.
// A single file change stays a `session.file` item. The group's key is derived from its first file, so it stays
// stable while the run grows and the row is not mounted anew for every file.
import type { TimelineItem } from './timeline';

/** The client-side kind of a run of file changes (no event kind: it has no dot, like `countdown`). */
export const FILE_GROUP = 'file-group';
const FILE = 'session.file';

export interface FileGroupBody {
  /** The `session.file` items of the run, in timeline order (at least two). */
  readonly files: readonly TimelineItem[];
  /** Time of the last file of the run (the span is `at` … `last`). */
  readonly last: number;
}

/** The timeline with every run of two or more consecutive `session.file` items of one session as one group item. */
export function groupFiles(items: readonly TimelineItem[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  let run: TimelineItem[] = [];
  const close = () => {
    const first = run[0]!;
    if (run.length === 1) out.push(first);
    else {
      const body: FileGroupBody = { files: run, last: run[run.length - 1]!.at };
      out.push({ ...first, key: `g${first.key}`, kind: FILE_GROUP, body });
    }
    run = [];
  };
  for (const item of items) {
    if (run.length > 0 && (item.kind !== FILE || item.sessionId !== run[0]!.sessionId)) close();
    if (item.kind === FILE) run.push(item);
    else out.push(item);
  }
  if (run.length > 0) close();
  return out;
}

const label = (item: TimelineItem): string => (item.subagent === null ? '' : item.subagent.path.join('.'));

function sameBody(a: TimelineItem, b: TimelineItem): boolean {
  if (a.body === b.body) return true;
  if (a.kind === FILE_GROUP) {
    const left = (a.body as FileGroupBody).files;
    const right = (b.body as FileGroupBody).files;
    return left.length === right.length && left.every((file, index) => sameItem(file, right[index]!));
  }
  // system rows and countdowns get a new small body object on every build; events keep the decrypted body object
  return a.source !== 'event' && JSON.stringify(a.body) === JSON.stringify(b.body);
}

/**
 * Whether two builds of an item render the same (memoized chat rows): the timeline is rebuilt on every store update,
 * so the item objects are new while the event bodies are the same objects.
 */
export function sameItem(a: TimelineItem, b: TimelineItem): boolean {
  return (
    a === b ||
    (a.key === b.key &&
      a.kind === b.kind &&
      a.at === b.at &&
      a.seq === b.seq &&
      a.code === b.code &&
      a.author === b.author &&
      a.sessionId === b.sessionId &&
      label(a) === label(b) &&
      sameBody(a, b))
  );
}

/** Rows rendered at first and kept while new items arrive; "Show earlier" adds `PAGE` rows. */
export const WINDOW = 150;
export const PAGE = 100;

/**
 * The index of the first rendered row (a window at the end of the list, so the DOM stays bounded however long the
 * chat is). `start` is the key of the first row rendered before: the window keeps it while new items arrive (no jump
 * under the reader) until `PAGE` more rows than `budget` are rendered; then it moves to the last `budget` rows.
 */
export function windowStart(items: readonly TimelineItem[], start: string | null, budget: number): number {
  const floor = Math.max(0, items.length - budget);
  const at = start === null ? -1 : items.findIndex((item) => item.key === start);
  if (at < 0 || items.length - at > budget + PAGE) return floor;
  return Math.min(at, floor);
}
