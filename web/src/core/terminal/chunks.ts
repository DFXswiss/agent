// Terminal chunks (05-runner.md section 7, 03-api.md section 7.2, ADR-016): the body of a `session.terminal` event is
// `{offset_ms_start, frames: [[t_ms, code, data]], cols, rows}` – asciicast v2 events with `t_ms` in milliseconds since
// the session's recording started (monotonic across recorder restarts), code `o` (output) or `m` (marker after a
// recorder restart). Only `o` data is written to the terminal.
import { isRecord } from '../registry';

/** Terminal size of the tmux pane when a chunk does not name one (05-runner.md section 7). */
export const DEFAULT_COLS = 160;
export const DEFAULT_ROWS = 48;
/** Contract bounds of `cols`/`rows`. */
export const MAX_SIZE = 1000;

export type FrameCode = 'o' | 'm';
export type Frame = readonly [tMs: number, code: FrameCode, data: string];

export interface ChunkBody {
  readonly offsetMs: number;
  readonly frames: readonly Frame[];
  readonly cols: number;
  readonly rows: number;
}

/** A verified chunk of one session at its `scope_seq`. */
export interface Chunk extends ChunkBody {
  readonly seq: number;
  /** Offset of the chunk's last frame (ms). */
  readonly endMs: number;
  /** Unix ms of the event header (`ts`: the device wrote the chunk at its last frame); null if unknown. */
  readonly atMs: number | null;
}

const isMs = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const isSize = (value: unknown): value is number => isMs(value) && value >= 1 && value <= MAX_SIZE;

function isFrame(value: unknown): value is Frame {
  return (
    Array.isArray(value) &&
    value.length === 3 &&
    isMs(value[0]) &&
    (value[1] === 'o' || value[1] === 'm') &&
    typeof value[2] === 'string'
  );
}

/** The chunk body of a decrypted `session.terminal` event, null if it does not follow the contract. */
export function parseChunk(body: unknown): ChunkBody | null {
  if (!isRecord(body) || !isMs(body.offset_ms_start) || !isSize(body.cols) || !isSize(body.rows)) return null;
  const frames = body.frames;
  if (!Array.isArray(frames) || frames.length === 0 || !frames.every(isFrame)) return null;
  return { offsetMs: body.offset_ms_start, frames, cols: body.cols, rows: body.rows };
}

/** A chunk at `seq` from its body, written at `atMs` (header time, if known). */
export function chunkAt(seq: number, body: ChunkBody, atMs: number | null = null): Chunk {
  const endMs = body.frames.reduce((max, frame) => Math.max(max, frame[0]), body.offsetMs);
  return { ...body, seq, endMs, atMs };
}

/**
 * The absolute time (Unix ms) of the recording offset `offsetMs` (06-web.md section 12.4: terminal positions as local
 * time): from the header time of the first chunk that reaches the offset, which was written at its last frame; null
 * without chunks or header times.
 */
export function absoluteAt(chunks: readonly Chunk[], offsetMs: number): number | null {
  const chunk = chunks.find((item) => item.endMs >= offsetMs && item.atMs !== null) ?? null;
  return chunk === null ? null : chunk.atMs! - (chunk.endMs - offsetMs);
}

/** The output (`o`) data of the frames of `chunks` whose offset lies in `(fromMs, toMs]`, in order. */
export function outputBetween(chunks: readonly Chunk[], fromMs: number, toMs: number): string {
  let text = '';
  for (const chunk of chunks) {
    if (chunk.endMs <= fromMs) continue;
    for (const [tMs, code, data] of chunk.frames) {
      if (code === 'o' && tMs > fromMs && tMs <= toMs) text += data;
    }
  }
  return text;
}

/** Everything the session wrote up to `toMs` (inclusive). */
export function outputUntil(chunks: readonly Chunk[], toMs: number): string {
  return outputBetween(chunks, -1, toMs);
}

/** All output of `chunks`. */
export function outputOf(chunks: readonly Chunk[]): string {
  return outputUntil(chunks, Number.MAX_SAFE_INTEGER);
}

export interface Range {
  readonly startMs: number;
  readonly endMs: number;
}

/** The recording range of the chunks (offset of the first chunk to the last frame); null without chunks. */
export function recordingRange(chunks: readonly Chunk[]): Range | null {
  if (chunks.length === 0) return null;
  const endMs = chunks.reduce((max, chunk) => Math.max(max, chunk.endMs), 0);
  return { startMs: Math.min(chunks[0]!.offsetMs, endMs), endMs };
}

/** The terminal geometry from the asciicast header of the newest chunk (160 × 48 without chunks). */
export function geometry(chunks: readonly Chunk[]): { readonly cols: number; readonly rows: number } {
  const last = chunks.at(-1);
  return last === undefined ? { cols: DEFAULT_COLS, rows: DEFAULT_ROWS } : { cols: last.cols, rows: last.rows };
}
