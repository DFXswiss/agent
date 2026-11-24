// Line-based diff of two whiteboard versions (06-web.md section 6.1: "diff to the previous version (line-based,
// client-side)"): a small Myers implementation (O((N+M)·D) time, O(D²) memory for the trace, at most `MAX_EDITS`
// changes) on the lines after the common prefix and suffix are cut off, so a typical edit of a long entry costs almost
// nothing. No dependency.
//
// Lines are split at `\n` (a `\r` before it stays part of the line); the result lists every line of both versions once,
// in order: `same` (in both), `del` (only in the old one), `add` (only in the new one). Within a changed block the
// deletions come before the additions.

export type DiffOp = 'same' | 'add' | 'del';

export interface DiffLine {
  readonly op: DiffOp;
  readonly text: string;
}

/** The lines of a text; the empty text has no lines. */
export function splitLines(text: string): string[] {
  return text === '' ? [] : text.split('\n');
}

/** The shortest edit script from `a` to `b` as lines (module comment). */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }
  const same = (lines: readonly string[]): DiffLine[] => lines.map((text) => ({ op: 'same', text }));
  return [...same(a.slice(0, start)), ...middle(a.slice(start, endA), b.slice(start, endB)), ...same(a.slice(endA))];
}

/**
 * Above this many changed lines the middle is shown as replaced (all old lines deleted, all new ones added): the
 * search costs O((N+M)·D) time and O(D²) memory, which a writer could otherwise drive up for every reader of the
 * history (a 64 KiB entry has up to 32 768 lines).
 */
export const MAX_EDITS = 2000;

/**
 * Myers' greedy forward search. `trace[d]` keeps only the band k ∈ [-d, d] of the furthest reaching x before step d
 * (index k + d), which is all the backtrack reads, so the trace holds O(D²) numbers.
 */
function middle(a: readonly string[], b: readonly string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  const max = Math.min(n + m, MAX_EDITS);
  const offset = max + 1;
  const v = new Array<number>(2 * max + 3).fill(0);
  const trace: number[][] = [];
  for (let d = 0; d <= max; d += 1) {
    trace.push(v.slice(offset - d, offset + d + 1));
    for (let k = -d; k <= d; k += 2) {
      const down = k === -d || (k !== d && v[offset + k - 1]! < v[offset + k + 1]!);
      let x = down ? v[offset + k + 1]! : v[offset + k - 1]! + 1;
      let y = x - k;
      while (x < n && y < m && a[x] === b[y]) {
        x += 1;
        y += 1;
      }
      v[offset + k] = x;
      if (x >= n && y >= m) return backtrack(trace, a, b);
    }
  }
  return [...a.map((text): DiffLine => ({ op: 'del', text })), ...b.map((text): DiffLine => ({ op: 'add', text }))];
}

function backtrack(trace: readonly number[][], a: readonly string[], b: readonly string[]): DiffLine[] {
  const out: DiffLine[] = [];
  let x = a.length;
  let y = b.length;
  for (let d = trace.length - 1; d >= 0; d -= 1) {
    const band = trace[d]!;
    const at = (k: number) => band[k + d]!;
    const k = x - y;
    const down = k === -d || (k !== d && at(k - 1) < at(k + 1));
    const prevK = down ? k + 1 : k - 1;
    const prevX = d === 0 ? 0 : at(prevK);
    const prevY = d === 0 ? 0 : prevX - prevK;
    while (x > prevX && y > prevY) {
      x -= 1;
      y -= 1;
      out.push({ op: 'same', text: a[x]! });
    }
    if (d === 0) break;
    if (down) {
      y -= 1;
      out.push({ op: 'add', text: b[y]! });
    } else {
      x -= 1;
      out.push({ op: 'del', text: a[x]! });
    }
  }
  return out.reverse();
}

/** The number of added and deleted lines. */
export function diffStats(lines: readonly DiffLine[]): { readonly added: number; readonly deleted: number } {
  return {
    added: lines.filter((line) => line.op === 'add').length,
    deleted: lines.filter((line) => line.op === 'del').length,
  };
}
