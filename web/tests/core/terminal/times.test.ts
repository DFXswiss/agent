// Terminal positions as absolute local times (06-web.md section 12.4): a chunk carries the header time of its event
// (written at its last frame); the time of a recording offset is derived from the first chunk that reaches it.
import { describe, expect, it } from 'vitest';
import { openTerminal, receive, type ChunkVerdict, verdictOf } from '../../../src/core/store/slices/terminal';
import { createStore } from '../../../src/core/store';
import { formatTime, parseTime } from '../../../src/core/encoding/time';
import { absoluteAt, chunkAt } from '../../../src/core/terminal/chunks';
import { body, entry, SESSION, TARGET } from './helpers';

const T0 = parseTime('2026-11-22T10:00:00.000Z');

describe('absolute positions', () => {
  const first = chunkAt(
    1,
    {
      offsetMs: 0,
      frames: [
        [0, 'o', 'a'],
        [400, 'o', 'b'],
      ],
      cols: 80,
      rows: 24,
    },
    T0 + 400,
  );
  const second = chunkAt(
    2,
    {
      offsetMs: 1000,
      frames: [
        [1000, 'o', 'c'],
        [1500, 'o', 'd'],
      ],
      cols: 80,
      rows: 24,
    },
    T0 + 1600,
  );
  const unknown = chunkAt(3, { offsetMs: 2000, frames: [[2000, 'o', 'e']], cols: 80, rows: 24 });

  it('derives the time of an offset from the chunk that reaches it', () => {
    expect(first.atMs).toBe(T0 + 400);
    expect(unknown.atMs).toBeNull();
    expect(absoluteAt([first, second], 0)).toBe(T0);
    expect(absoluteAt([first, second], 400)).toBe(T0 + 400);
    expect(absoluteAt([first, second], 1200)).toBe(T0 + 1300);
  });

  it('knows no time without chunks, beyond the recording or without header times', () => {
    expect(absoluteAt([], 0)).toBeNull();
    expect(absoluteAt([first, second], 1501)).toBeNull();
    expect(absoluteAt([unknown], 2000)).toBeNull();
    expect(absoluteAt([unknown, second], 1200)).toBe(T0 + 1300);
  });

  it('takes the header time of the chunk event', () => {
    const store = createStore();
    openTerminal(store, TARGET);
    const opened = store.getState().terminal.sessions[SESSION]!;
    const verdict = verdictOf(entry(1));
    expect(verdict).toMatchObject({ status: 'verified' });
    const timed = { ...verdict, body: body(0, [[0, 'x']]), ts: formatTime(T0) } as ChunkVerdict;
    expect(receive(opened, timed).held[0]!.atMs).toBe(T0);
    const untimed = { ...timed, ts: 'yesterday' } as ChunkVerdict;
    expect(receive(opened, untimed).held[0]!.atMs).toBeNull();
  });
});
