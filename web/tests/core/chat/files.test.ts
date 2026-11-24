// U-WEB-chat-files (HOT-05): runs of `session.file` items become one `file-group` item per session run, the item
// comparison of the memoized chat rows.
import { describe, expect, it } from 'vitest';
import { FILE_GROUP, type FileGroupBody, groupFiles, sameItem } from '../../../src/core/chat/files';
import type { TimelineItem } from '../../../src/core/chat/timeline';
import type { SessionNode } from '../../../src/core/sessions/tree';

let seq = 0;

function item(kind: string, overrides: Partial<TimelineItem> = {}): TimelineItem {
  seq += 1;
  return {
    key: `e${seq}`,
    kind,
    source: 'event',
    at: 1000 + seq,
    seq,
    sessionId: 'main',
    author: 'u1',
    body: { n: seq },
    code: null,
    subagent: null,
    ...overrides,
  };
}

const file = (overrides: Partial<TimelineItem> = {}) => item('session.file', overrides);
const node = (path: number[]): SessionNode => ({ entry: {} as never, depth: path.length, path, children: [] });

describe('groupFiles', () => {
  it('groups consecutive file changes of one session and keeps single ones and other items', () => {
    const prompt = item('prompt', { sessionId: null });
    const a = file();
    const b = file();
    const c = file();
    const output = item('session.output');
    const single = file();
    const other = file({ sessionId: 'child' });
    const d = file();
    const e = file();
    const grouped = groupFiles([prompt, a, b, c, output, single, other, d, e]);
    expect(grouped.map((row) => [row.kind, row.key])).toEqual([
      ['prompt', prompt.key],
      [FILE_GROUP, `g${a.key}`],
      ['session.output', output.key],
      ['session.file', single.key],
      ['session.file', other.key],
      [FILE_GROUP, `g${d.key}`],
    ]);
    const group = grouped[1]!;
    expect(group.at).toBe(a.at);
    expect(group.sessionId).toBe('main');
    expect(group.body).toEqual({ files: [a, b, c], last: c.at });
    expect(groupFiles([])).toEqual([]);
  });

  it('keeps the key of a growing run', () => {
    const a = file();
    const b = file();
    const before = groupFiles([a, b]);
    const after = groupFiles([a, b, file(), file()]);
    expect(after).toHaveLength(1);
    expect(after[0]!.key).toBe(before[0]!.key);
    expect((after[0]!.body as FileGroupBody).files).toHaveLength(4);
  });
});

describe('sameItem', () => {
  it('compares the rendered content, not the object', () => {
    const a = item('session.output');
    expect(sameItem(a, a)).toBe(true);
    expect(sameItem(a, { ...a })).toBe(true);
    for (const change of [
      { key: 'x' },
      { kind: 'prompt' },
      { at: 1 },
      { seq: -1 },
      { code: 'c' },
      { author: 'u2' },
      { sessionId: 'other' },
      { subagent: node([1]) },
      { body: { ...(a.body as object) } },
    ])
      expect(sameItem(a, { ...a, ...change })).toBe(false);
    const child = item('session.output', { subagent: node([1, 2]) });
    expect(sameItem(child, { ...child, subagent: node([1, 2]) })).toBe(true);
    expect(sameItem(child, { ...child, subagent: node([1, 3]) })).toBe(false);
  });

  it('compares system rows by their small body and groups by their files', () => {
    const row = item('session.status', { source: 'transition', body: { state: 'failed', reason: null } });
    expect(sameItem(row, { ...row, body: { state: 'failed', reason: null } })).toBe(true);
    expect(sameItem(row, { ...row, body: { state: 'stopped', reason: null } })).toBe(false);
    const [a, b, c] = [file(), file(), file()];
    const [group] = groupFiles([a, b]);
    expect(sameItem(group!, groupFiles([{ ...a }, { ...b }])[0]!)).toBe(true);
    expect(sameItem(group!, { ...group!, body: { files: [a, { ...b, body: {} }], last: b.at } })).toBe(false);
    expect(sameItem(group!, { ...group!, body: { files: [a, b, c], last: b.at } })).toBe(false);
  });
});
