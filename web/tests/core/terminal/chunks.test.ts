// Terminal chunks: contract parsing, output by offset (replay up to an offset), recording range and geometry.
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
