// The changed parts of a line diff (06-web.md section 6.1, ADR-063 section 1): only the hunks around changed lines with
// `CONTEXT` unchanged lines before and after (as in a unified diff), so a small edit of a long entry shows a few lines
// instead of the whole text. Hunks whose context touches or overlaps are merged. `diffText` writes the hunks as one
// text (a header `@@ -old +new @@` per hunk, every line with its sign), which the history clips and offers for download.
import type { DiffLine, DiffOp } from '../../core/whiteboards/diff';

/** Unchanged lines shown before and after a change. */
export const CONTEXT = 3;

export interface Hunk {
  /** First line of the hunk in the old version (1-based; the line before it when the hunk has no old lines). */
  readonly oldStart: number;
  readonly oldCount: number;
  readonly newStart: number;
  readonly newCount: number;
  readonly lines: readonly DiffLine[];
}

/** The hunks of `lines` with `context` unchanged lines around each change; none when nothing changed. */
export function hunks(lines: readonly DiffLine[], context: number = CONTEXT): Hunk[] {
  // the ranges [from, to) of lines to show, merged when they touch
  const ranges: [number, number][] = [];
  lines.forEach((line, index) => {
    if (line.op === 'same') return;
    const from = Math.max(0, index - context);
    const to = Math.min(lines.length, index + context + 1);
    const last = ranges[ranges.length - 1];
    if (last !== undefined && from <= last[1]) last[1] = to;
    else ranges.push([from, to]);
  });
  const out: Hunk[] = [];
  let oldLine = 0;
  let newLine = 0;
  let at = 0;
  const step = (op: DiffOp) => {
    if (op !== 'add') oldLine += 1;
    if (op !== 'del') newLine += 1;
  };
  for (const [from, to] of ranges) {
    for (; at < from; at += 1) step(lines[at]!.op);
    const part = lines.slice(from, to);
    const oldCount = part.filter((line) => line.op !== 'add').length;
    const newCount = part.filter((line) => line.op !== 'del').length;
    out.push({
      oldStart: oldCount === 0 ? oldLine : oldLine + 1,
      oldCount,
      newStart: newCount === 0 ? newLine : newLine + 1,
      newCount,
      lines: part,
    });
  }
  return out;
}

/** The sign of a line in the diff text (not colour alone: every line carries it). */
export const SIGN: Readonly<Record<DiffOp, string>> = { same: ' ', add: '+', del: '−' };

/** The header line of a hunk. */
export const hunkHeader = (hunk: Hunk): string =>
  `@@ -${hunk.oldStart},${hunk.oldCount} +${hunk.newStart},${hunk.newCount} @@`;

/** The hunks as text: per hunk its header, then every line as `<sign> <text>`. */
export function diffText(parts: readonly Hunk[]): string {
  return parts
    .flatMap((hunk) => [hunkHeader(hunk), ...hunk.lines.map((line) => `${SIGN[line.op]} ${line.text}`)])
    .join('\n');
}

export type TextLineKind = DiffOp | 'hunk';

/** What a line of `diffText` (or a clipped start of one) is, by its first character. */
export function lineKind(line: string): TextLineKind {
  if (line.startsWith('@')) return 'hunk';
  if (line.startsWith(SIGN.add)) return 'add';
  if (line.startsWith(SIGN.del)) return 'del';
  return 'same';
}
