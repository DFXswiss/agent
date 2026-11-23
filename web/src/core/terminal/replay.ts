// History mode of the terminal (06-web.md section 5.5): a time slider over the recording range. Moving it replays the
// output from the start up to the chosen offset (the view writes it into an off-screen terminal and then shows it);
// playback continues from there at 1×, 4× or 16× the recorded speed. `Player` is the clock of the playback: every
// `TICK_MS` it hands the output between the previous and the new position to `onOutput` and stops at the end.
import { SYSTEM_TIMERS, type Timers } from '../net/sse';
import { type Chunk, outputBetween, recordingRange } from './chunks';

export const SPEEDS = [1, 4, 16] as const;
export type Speed = (typeof SPEEDS)[number];
export const TICK_MS = 50;

export interface PlayerOptions {
  /** The recorded chunks (read on every tick: chunks that arrive meanwhile extend the range). */
  readonly chunks: () => readonly Chunk[];
  readonly onOutput: (data: string) => void;
  readonly onPosition: (positionMs: number, playing: boolean) => void;
  readonly timers?: Timers;
  readonly now: () => number;
}

export class Player {
  readonly #options: PlayerOptions;
  readonly #timers: Timers;
  #position = 0;
  #speed: Speed = 1;
  #timer: unknown = null;
  #last = 0;

  constructor(options: PlayerOptions) {
    this.#options = options;
    this.#timers = options.timers ?? SYSTEM_TIMERS;
  }

  get position(): number {
    return this.#position;
  }

  get speed(): Speed {
    return this.#speed;
  }

  get playing(): boolean {
    return this.#timer !== null;
  }

  /** Sets the position (after the view replayed the output up to it); stops a running playback. */
  seek(positionMs: number): void {
    this.pause();
    this.#position = Math.max(0, positionMs);
    this.#options.onPosition(this.#position, false);
  }

  setSpeed(speed: Speed): void {
    this.#speed = speed;
  }

  /** Plays from the current position; at the end of the recording it stops. */
  play(): void {
    if (this.playing) return;
    const end = recordingRange(this.#options.chunks())?.endMs ?? 0;
    if (this.#position >= end) return;
    this.#last = this.#options.now();
    this.#arm();
    this.#options.onPosition(this.#position, true);
  }

  pause(): void {
    if (this.#timer === null) return;
    this.#timers.clearTimeout(this.#timer);
    this.#timer = null;
    this.#options.onPosition(this.#position, false);
  }

  #arm(): void {
    this.#timer = this.#timers.setTimeout(() => this.#tick(), TICK_MS);
  }

  #tick(): void {
    const now = this.#options.now();
    const chunks = this.#options.chunks();
    const end = recordingRange(chunks)?.endMs ?? 0;
    const next = Math.min(end, this.#position + (now - this.#last) * this.#speed);
    this.#last = now;
    const data = outputBetween(chunks, this.#position, next);
    this.#position = next;
    if (data !== '') this.#options.onOutput(data);
    if (next >= end) {
      this.#timer = null;
      this.#options.onPosition(next, false);
      return;
    }
    this.#arm();
    this.#options.onPosition(next, true);
  }
}
