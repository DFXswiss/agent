// U-WEB-ui-whiteboard-hunks: the changed hunks of a whiteboard diff with three lines of context, their text form and
// the kind of a (clipped) line of that text.
import { describe, expect, it } from 'vitest';
import { diffLines, type DiffLine } from '../../src/core/whiteboards/diff';
import { CONTEXT, diffText, hunkHeader, hunks, lineKind } from '../../src/ui/whiteboard/hunks';

const numbered = (count: number, change: (index: number) => string | null = () => null) =>
  Array.from({ length: count }, (_, index) => change(index) ?? `line ${index + 1}`).join('\n');

const ops = (lines: readonly DiffLine[]) => lines.map((line) => line.op[0]).join('');

describe('hunks', () => {
  it('has none when nothing changed', () => {
    expect(CONTEXT).toBe(3);
    expect(hunks(diffLines('a\nb', 'a\nb'))).toEqual([]);
    expect(hunks([])).toEqual([]);
    expect(diffText([])).toBe('');
  });

  it('shows a change in the middle with three lines of context on both sides', () => {
    const before = numbered(100);
    const after = numbered(100, (index) => (index === 49 ? 'changed' : null));
    const [hunk, ...rest] = hunks(diffLines(before, after));
    expect(rest).toEqual([]);
    expect(ops(hunk!.lines)).toBe('sssdasss');
    expect(hunk).toMatchObject({ oldStart: 47, oldCount: 7, newStart: 47, newCount: 7 });
    expect(hunkHeader(hunk!)).toBe('@@ -47,7 +47,7 @@');
    expect(hunk!.lines[0]!.text).toBe('line 47');
    expect(hunk!.lines[7]!.text).toBe('line 53');
  });

  it('clips the context at both ends of the text', () => {
    const before = numbered(10);
    const after = numbered(10, (index) => (index === 0 || index === 9 ? 'x' : null));
    const parts = hunks(diffLines(before, after));
    expect(parts).toHaveLength(2);
    expect(ops(parts[0]!.lines)).toBe('dasss');
    expect(parts[0]).toMatchObject({ oldStart: 1, oldCount: 4, newStart: 1, newCount: 4 });
    expect(ops(parts[1]!.lines)).toBe('sssda');
    expect(parts[1]).toMatchObject({ oldStart: 7, oldCount: 4, newStart: 7, newCount: 4 });
  });

  it('merges changes whose context touches and keeps far ones apart', () => {
    // six unchanged lines between two changes: the contexts meet, one hunk
    const near = hunks(
      diffLines(
        numbered(20),
        numbered(20, (index) => (index === 5 || index === 12 ? 'x' : null)),
      ),
    );
    expect(near).toHaveLength(1);
    expect(ops(near[0]!.lines)).toBe('sssdassssssdasss');
    // seven unchanged lines between: two hunks
    const far = hunks(
      diffLines(
        numbered(20),
        numbered(20, (index) => (index === 5 || index === 13 ? 'x' : null)),
      ),
    );
    expect(far).toHaveLength(2);
    expect(far.map((hunk) => [hunk.oldStart, hunk.newStart])).toEqual([
      [3, 3],
      [11, 11],
    ]);
  });

  it('numbers hunks of only added or only deleted lines like a unified diff', () => {
    expect(hunks(diffLines('', 'a\nb'))).toEqual([
      { oldStart: 0, oldCount: 0, newStart: 1, newCount: 2, lines: diffLines('', 'a\nb') },
    ]);
    expect(hunks(diffLines('a\nb', ''))).toEqual([
      { oldStart: 1, oldCount: 2, newStart: 0, newCount: 0, lines: diffLines('a\nb', '') },
    ]);
    // an insertion after line 10 of 20: the old side has the context only
    const inserted = hunks(
      diffLines(numbered(20), `${numbered(10)}\nnew\n${numbered(20).split('\n').slice(10).join('\n')}`),
    );
    expect(inserted).toEqual([expect.objectContaining({ oldStart: 8, oldCount: 6, newStart: 8, newCount: 7 })]);
    // with no context, an insertion has no old lines: its start is the line before it
    expect(hunks(diffLines('a\nc', 'a\nb\nc'), 0)).toEqual([
      { oldStart: 1, oldCount: 0, newStart: 2, newCount: 1, lines: [{ op: 'add', text: 'b' }] },
    ]);
  });

  it('writes the hunks as text and reads the kind of each line back', () => {
    const text = diffText(hunks(diffLines('a\nb\nc', 'a\nB\nc')));
    expect(text).toBe('@@ -1,3 +1,3 @@\n  a\n− b\n+ B\n  c');
    expect(text.split('\n').map(lineKind)).toEqual(['hunk', 'same', 'del', 'add', 'same']);
    // a clipped start of a line keeps its kind
    expect(lineKind('+')).toBe('add');
    expect(lineKind('')).toBe('same');
  });
});
