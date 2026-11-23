// Test helpers of the terminal core: chunk bodies, `events` entries of a workgroup scope and verdicts.
import type { EventEntry } from '../../../src/core/store/slices/events';

export const ORG = '01930000-0000-7000-8000-0000000000aa';
export const WG = '01930000-0000-7000-8000-0000000000bb';
export const SESSION = '01930000-0000-7000-8000-0000000000c1';
export const OTHER = '01930000-0000-7000-8000-0000000000c2';
export const TARGET = { org: ORG, wg: WG, session: SESSION };

/** A chunk body with one output frame per `[t, data]`. */
export function body(offset: number, frames: [number, string][], size: [number, number] = [160, 48]) {
  return {
    offset_ms_start: offset,
    frames: frames.map(([t, data]) => [t, 'o', data]),
    cols: size[0],
    rows: size[1],
  };
}

/** An `events` entry at `seq` (verified unless `status` says otherwise). */
export function entry(
  seq: number,
  options: {
    kind?: string;
    session?: string;
    status?: EventEntry['status'];
    body?: unknown;
    id?: string;
    code?: string;
  } = {},
): EventEntry {
  const status = options.status ?? 'verified';
  const settled = status === 'verified' || status === 'locked';
  return {
    org: ORG,
    scopeId: WG,
    scopeSeq: seq,
    receivedAt: null,
    event: { header: { id: options.id ?? `id-${seq}` } },
    status,
    code: status === 'unverifiable' ? (options.code ?? 'signature_invalid') : null,
    header: settled
      ? {
          id: options.id ?? `id-${seq}`,
          kind: options.kind ?? 'session.terminal',
          session: options.session ?? SESSION,
        }
      : null,
    body: status === 'verified' ? (options.body ?? body(seq * 100, [[seq * 100, `c${seq}`]])) : null,
  };
}

/** A non-terminal event (accounts for its seq). */
export const other = (seq: number): EventEntry => entry(seq, { kind: 'session.output', body: { text: 'x' } });
