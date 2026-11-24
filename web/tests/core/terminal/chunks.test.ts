// Terminal chunks: contract parsing, output by offset (replay up to an offset, bounded replays), recording range and
// geometry.
import { describe, expect, it } from 'vitest';
import {
  chunkAt,
  DEFAULT_COLS,
  DEFAULT_ROWS,
  geometry,
  outputBetween,
  outputOf,
  outputUntil,
  parseChunk,
  recordingRange,
  REPLAY_CHARS,
  replayStart,
} from '../../../src/core/terminal/chunks';
import { body } from './helpers';

describe('parseChunk', () => {
  it('accepts the contract body with output and marker frames', () => {
    const parsed = parseChunk({
      offset_ms_start: 10,
      frames: [
        [10, 'o', 'a'],
        [12, 'm', 'recorder restarted'],
      ],
      cols: 80,
      rows: 24,
    });
    expect(parsed).toEqual({
      offsetMs: 10,
      frames: [
        [10, 'o', 'a'],
        [12, 'm', 'recorder restarted'],
      ],
      cols: 80,
      rows: 24,
    });
  });

  it.each([
    ['no object', null],
    ['an array', []],
    ['negative offset', { ...body(0, [[0, 'a']]), offset_ms_start: -1 }],
    ['fractional offset', { ...body(0, [[0, 'a']]), offset_ms_start: 1.5 }],
    ['zero columns', body(0, [[0, 'a']], [0, 48])],
    ['too many rows', body(0, [[0, 'a']], [160, 1001])],
    ['string rows', { ...body(0, [[0, 'a']]), rows: '48' }],
    ['no frames', body(0, [])],
    ['frames not a list', { ...body(0, [[0, 'a']]), frames: {} }],
    ['short frame', { ...body(0, [[0, 'a']]), frames: [[0, 'o']] }],
    ['frame not a list', { ...body(0, [[0, 'a']]), frames: ['x'] }],
    ['negative time', { ...body(0, [[0, 'a']]), frames: [[-1, 'o', 'a']] }],
    ['input frame', { ...body(0, [[0, 'a']]), frames: [[0, 'i', 'a']] }],
    ['data not text', { ...body(0, [[0, 'a']]), frames: [[0, 'o', 1]] }],
  ])('refuses %s', (_name, value) => {
    expect(parseChunk(value)).toBeNull();
  });
});

describe('output by offset', () => {
  const chunks = [
    chunkAt(
      3,
      parseChunk(
        body(0, [
          [0, 'a'],
          [100, 'b'],
        ]),
      )!,
    ),
    chunkAt(
      7,
      parseChunk({
        offset_ms_start: 200,
        frames: [
          [200, 'o', 'c'],
          [250, 'm', 'marker'],
          [300, 'o', 'd'],
        ],
        cols: 80,
        rows: 24,
      })!,
    ),
  ];

  it('ends a chunk at its last frame', () => {
    expect(chunks.map((chunk) => [chunk.seq, chunk.endMs])).toEqual([
      [3, 100],
      [7, 300],
    ]);
    expect(chunkAt(1, { offsetMs: 50, frames: [[10, 'o', 'x']], cols: 1, rows: 1 }).endMs).toBe(50);
  });

  it('counts the output characters and keeps the event ID', () => {
    expect(chunks.map((chunk) => [chunk.id, chunk.chars])).toEqual([
      ['', 2],
      ['', 2],
    ]);
    expect(chunkAt(1, { offsetMs: 0, frames: [[0, 'o', 'xyz']], cols: 1, rows: 1 }, null, 'e1')).toMatchObject({
      id: 'e1',
      chars: 3,
    });
  });

  it('replays up to an offset, between offsets and everything, without markers', () => {
    expect(outputUntil(chunks, 0)).toBe('a');
    expect(outputUntil(chunks, 99)).toBe('a');
    expect(outputUntil(chunks, 250)).toBe('abc');
    expect(outputBetween(chunks, 100, 300)).toBe('cd');
    expect(outputBetween(chunks, 0, 100)).toBe('b');
    expect(outputOf(chunks)).toBe('abcd');
  });

  it('gives the recording range and the newest geometry', () => {
    expect(recordingRange([])).toBeNull();
    expect(recordingRange(chunks)).toEqual({ startMs: 0, endMs: 300 });
    expect(geometry([])).toEqual({ cols: DEFAULT_COLS, rows: DEFAULT_ROWS });
    expect(geometry(chunks)).toEqual({ cols: 80, rows: 24 });
  });
});

describe('bounded replays', () => {
  // four chunks of 4 characters at 0, 100, 200 and 300 ms
  const chunks = [0, 1, 2, 3].map((index) =>
    chunkAt(index + 1, parseChunk(body(index * 100, [[index * 100, `${'abcd'[index]!.repeat(4)}`]]))!),
  );

  it('starts at the newest chunk boundary within the bound', () => {
    expect(REPLAY_CHARS).toBe(2 * 1024 * 1024);
    expect(replayStart(chunks, 300, 100)).toBe(0);
    expect(replayStart(chunks, 300, 8)).toBe(2);
    // chunks after the offset do not count
    expect(replayStart(chunks, 150, 4)).toBe(1);
    // one chunk is always replayed, even beyond the bound
    expect(replayStart(chunks, 300, 1)).toBe(3);
    expect(replayStart([], 300, 1)).toBe(0);
  });

  it('replays up to an offset and the newest output within the bound', () => {
    expect(outputUntil(chunks, 250, 8)).toBe('bbbbcccc');
    expect(outputUntil(chunks, 250)).toBe('aaaabbbbcccc');
    expect(outputOf(chunks, 4)).toBe('dddd');
    expect(outputOf(chunks)).toBe('aaaabbbbccccdddd');
  });
});
