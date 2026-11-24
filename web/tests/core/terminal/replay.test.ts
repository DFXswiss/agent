// The playback clock of the history mode: seek, play at 1×/4×/16× with the output between positions, pause, the end of
// the recording and chunks that arrive during playback.
import { describe, expect, it } from 'vitest';
import { type Chunk, chunkAt, parseChunk } from '../../../src/core/terminal/chunks';
import { Player, SPEEDS, TICK_MS } from '../../../src/core/terminal/replay';
import { ManualTimers } from '../net/stream';
import { body } from './helpers';

function setup(initial: Chunk[]) {
  const timers = new ManualTimers();
  let chunks = initial;
  const output: string[] = [];
  const positions: [number, boolean][] = [];
  const player = new Player({
    chunks: () => chunks,
    onOutput: (data) => output.push(data),
    onPosition: (position, playing) => positions.push([position, playing]),
    timers,
    now: () => timers.now,
  });
  return { timers, player, output, positions, setChunks: (next: Chunk[]) => (chunks = next) };
}

const chunk = (seq: number, frames: [number, string][]) => chunkAt(seq, parseChunk(body(frames[0]![0], frames))!);

describe('player', () => {
  it('plays from the sought position at the chosen speed and stops at the end', () => {
    const t = setup([
      chunk(1, [
        [0, 'a'],
        [100, 'b'],
        [400, 'c'],
      ]),
    ]);
    expect(SPEEDS).toEqual([1, 4, 16]);
    expect(t.player.speed).toBe(1);
    t.player.seek(-5);
    expect(t.player.position).toBe(0);
    t.player.play();
    expect(t.player.playing).toBe(true);
    t.player.play();
    t.timers.advance(TICK_MS);
    expect(t.output).toEqual([]);
    t.timers.advance(TICK_MS);
    expect(t.output).toEqual(['b']);
    t.player.setSpeed(16);
    t.timers.advance(TICK_MS);
    expect(t.output).toEqual(['b', 'c']);
    expect(t.player.playing).toBe(false);
    expect(t.player.position).toBe(400);
    expect(t.positions.at(-1)).toEqual([400, false]);
    // at the end, play does nothing
    t.player.play();
    expect(t.player.playing).toBe(false);
  });

  it('pauses and resumes; seeking stops the playback', () => {
    const t = setup([
      chunk(1, [
        [0, 'a'],
        [1000, 'b'],
      ]),
    ]);
    t.player.seek(0);
    t.player.pause();
    t.player.setSpeed(4);
    t.player.play();
    t.timers.advance(TICK_MS);
    expect(t.player.position).toBe(200);
    t.player.pause();
    expect(t.positions.at(-1)).toEqual([200, false]);
    t.timers.advance(10 * TICK_MS);
    expect(t.player.position).toBe(200);
    t.player.play();
    t.player.seek(900);
    expect(t.player.playing).toBe(false);
    expect(t.timers.pending).toEqual([]);
  });

  it('follows chunks that arrive during playback and plays nothing without chunks', () => {
    const t = setup([
      chunk(1, [
        [0, 'a'],
        [100, 'b'],
      ]),
    ]);
    t.player.seek(0);
    t.player.play();
    t.setChunks([
      chunk(1, [
        [0, 'a'],
        [100, 'b'],
      ]),
      chunk(2, [
        [150, 'c'],
        [5000, 'd'],
      ]),
    ]);
    t.player.setSpeed(16);
    t.timers.advance(TICK_MS);
    expect(t.output).toEqual(['bc']);
    expect(t.player.playing).toBe(true);
    // the chunks vanish during playback (the view closed): the playback ends
    t.setChunks([]);
    t.timers.advance(TICK_MS);
    expect(t.player.playing).toBe(false);
    const plain = new Player({
      chunks: () => [],
      onOutput: () => undefined,
      onPosition: () => undefined,
      now: () => 0,
    });
    expect(plain.playing).toBe(false);
    const empty = setup([]);
    empty.player.play();
    expect(empty.player.playing).toBe(false);
  });
});
