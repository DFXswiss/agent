// The line diff of whiteboard versions (06-web.md section 6.1): the shortest edit script, deletions before additions
// within a change, common prefix and suffix kept, empty texts, and a reconstruction property on generated texts.
import { describe, expect, it } from 'vitest';
import { diffLines, diffStats, type DiffLine, MAX_EDITS, splitLines } from '../../../src/core/whiteboards/diff';

const show = (lines: readonly DiffLine[]) =>
  lines.map((line) => `${line.op === 'same' ? ' ' : line.op === 'add' ? '+' : '-'}${line.text}`);

/** The old and the new text from a diff (every line of both appears once, in order). */
function rebuild(lines: readonly DiffLine[]): [string, string] {
  const before = lines.filter((line) => line.op !== 'add').map((line) => line.text);
  const after = lines.filter((line) => line.op !== 'del').map((line) => line.text);
  return [before.join('\n'), after.join('\n')];
}

describe('splitLines', () => {
  it('splits at newlines and keeps carriage returns; the empty text has no lines', () => {
    expect(splitLines('')).toEqual([]);
    expect(splitLines('a')).toEqual(['a']);
    expect(splitLines('a\r\nb\n')).toEqual(['a\r', 'b', '']);
  });
});

describe('diffLines', () => {
  it('reports identical texts as unchanged', () => {
    expect(show(diffLines('a\nb', 'a\nb'))).toEqual([' a', ' b']);
    expect(diffLines('', '')).toEqual([]);
  });

  it('adds and deletes whole texts', () => {
    expect(show(diffLines('', 'a\nb'))).toEqual(['+a', '+b']);
    expect(show(diffLines('a\nb', ''))).toEqual(['-a', '-b']);
  });

  it('keeps the common prefix and suffix and puts deletions before additions', () => {
    expect(show(diffLines('# Plan\nold line\nend', '# Plan\nnew line\nend'))).toEqual([
      ' # Plan',
      '-old line',
      '+new line',
      ' end',
    ]);
  });

  it('finds the shortest script in the middle', () => {
    const lines = diffLines('a\nb\nc\na\nb\nb\na', 'c\nb\na\nb\na\nc');
    expect(diffStats(lines).added + diffStats(lines).deleted).toBe(5);
    expect(rebuild(lines)).toEqual(['a\nb\nc\na\nb\nb\na', 'c\nb\na\nb\na\nc']);
  });

  it('handles insertions and deletions in the middle of a longer text', () => {
    expect(show(diffLines('1\n2\n3\n4\n5', '1\n2\nx\n3\n5'))).toEqual([' 1', ' 2', '+x', ' 3', '-4', ' 5']);
  });

  it('rebuilds both texts for generated pairs', () => {
    let seed = 7;
    const random = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    const text = () =>
      Array.from({ length: Math.floor(random() * 12) }, () => 'abcd'[Math.floor(random() * 4)]!).join('\n');
    for (let round = 0; round < 200; round += 1) {
      const before = text();
      const after = text();
      const lines = diffLines(before, after);
      expect(rebuild(lines)).toEqual([before, after]);
      const stats = diffStats(lines);
      expect(stats.added - stats.deleted).toBe(splitLines(after).length - splitLines(before).length);
    }
  });
});

describe('limits', () => {
  it('shows a rewrite beyond MAX_EDITS as replaced, quickly and with little memory', () => {
    const before = Array.from({ length: 30000 }, (_, i) => `a${i}`).join('\n');
    const after = Array.from({ length: 30000 }, (_, i) => `b${i}`).join('\n');
    const started = Date.now();
    const lines = diffLines(before, after);
    expect(Date.now() - started).toBeLessThan(5000);
    expect(diffStats(lines)).toEqual({ added: 30000, deleted: 30000 });
    expect(lines[0]).toEqual({ op: 'del', text: 'a0' });
    expect(lines[30000]).toEqual({ op: 'add', text: 'b0' });
  });

  it('still finds the shortest script just below the limit', () => {
    const before = Array.from({ length: MAX_EDITS / 2 }, (_, i) => `x${i}`).join('\n');
    const after = Array.from({ length: MAX_EDITS / 2 }, (_, i) => `y${i}`).join('\n');
    expect(diffStats(diffLines(before, after))).toEqual({ added: MAX_EDITS / 2, deleted: MAX_EDITS / 2 });
    expect(rebuild(diffLines(`k\n${before}\nm`, `k\n${after}\nm`))).toEqual([`k\n${before}\nm`, `k\n${after}\nm`]);
  });
});
