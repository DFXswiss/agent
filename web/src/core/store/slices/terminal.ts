// Slice `terminal` (06-web.md sections 2.3 and 5.5, ADR-016, ADR-086 section 1): the terminal chunks of the watched
// sessions only (a session leaves the slice when its view closes). Per session:
//
// - `chunks`: the **tail** – verified chunks applied strictly in `scope_seq` order (the view writes them in this
//   order). It starts with the newest page of the session, grows by push and keeps the newest `KEEP_CHARS` of output:
//   beyond it the oldest chunks leave memory (in steps of an eighth, so that the drop is rare) and `earlier` is the
//   `before` cursor of the output older than the first chunk in memory (null: the tail starts with the recording).
//   `epoch` counts the replacements of the tail by a newer newest page (a gap too long to repair): the view rewrites
//   its screen then;
// - `through`: every chunk of the session at or below this `scope_seq` is known (applied or unverifiable) – set by the
//   history pull (its last page's `head`) and moved by live chunks;
// - `held`: live chunks beyond a possible gap. Chunks of one session are a subsequence of the workgroup scope, so a
//   live chunk at `seq` is next only if every `scope_seq` between `through` and `seq` is accounted for, i.e. a verified
//   event that is not a terminal chunk of this session (`accounted`). Anything else – a missing seq (a lost frame, a
//   chunk of an unwatched session), a pending, locked or unverifiable event – may hide a chunk of this session, so the
//   held chunks wait and the watcher pulls (`core/terminal/watch.ts`) before anything further is rendered;
// - `bad`: chunks the reader refused or that break the contract (shown as a notice, never as output, never dropped);
// - `blockedAt`: a pulled chunk that could not be read yet (pending log, missing epoch key): nothing at or beyond it is
//   applied until a later pull reads it;
// - `history`: the window of the history mode (null in Follow): a copy of the tail when History was chosen, extended
//   at the front by older pages on demand (`applyEarlier`, newest first from the hub, so nothing older is lost) and at
//   the end by the live chunks while it stays below `HISTORY_CHARS`. A window that would exceed it drops its newest
//   chunks, or stops taking live chunks, and says so (`cut`): Follow shows the newest output again.
//
// Copies: the arrays `chunks`, `bad`, the window's `chunks` and the maps `ids` belong to the newest entry and grow in
// place (an update creates a new entry object that shares them; older entry objects must not be read for them). No
// update copies the chunk list – only a change of mode or a page loaded on demand does.
//
// The slice also handles the frame `terminals_refused {sessions}` of the live channel (the hub would not watch them).
// The steps and actions on the entries live in `core/terminal/entries.ts`: only the terminal view uses them, so they
// are loaded with it, not with the first load (06-web.md section 12.2).
import type { Chunk } from '../../terminal/chunks';
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

export const TERMINAL_KIND = 'session.terminal';

export type TerminalStatus = 'loading' | 'ready' | 'error';

export interface TerminalTarget {
  readonly org: string;
  readonly wg: string;
  readonly session: string;
}

export interface BadChunk {
  readonly seq: number;
  readonly code: string;
}

/** Characters of output the tail keeps (ADR-086 section 1: 8 MiB of output in Follow mode). */
export const KEEP_CHARS = 8 * 1024 * 1024;
/** Characters of output the history window holds at most. */
export const HISTORY_CHARS = 16 * 1024 * 1024;

export interface HistoryWindow {
  /** The chunks of the window, ascending. */
  readonly chunks: readonly Chunk[];
  readonly chars: number;
  /** `before` cursor of the output older than the window; null at the start of the recording. */
  readonly before: number | null;
  /** Output after the window exists but is not in it (dropped for memory, or a replaced tail). */
  readonly cut: boolean;
  readonly bad: readonly BadChunk[];
  /** Loading an earlier page, or why the last load failed. */
  readonly status: 'ready' | 'loading' | 'error';
  readonly code: string | null;
  /** Event ID → `scope_seq` of the window's chunks. */
  readonly ids: ReadonlyMap<string, number>;
}

export interface TerminalEntry extends TerminalTarget {
  readonly status: TerminalStatus;
  readonly code: string | null;
  readonly chunks: readonly Chunk[];
  /** Characters of output in `chunks`. */
  readonly chars: number;
  /** `before` cursor of the output older than `chunks` (not in memory); null if the tail starts the recording. */
  readonly earlier: number | null;
  readonly epoch: number;
  readonly through: number;
  readonly held: readonly Chunk[];
  readonly bad: readonly BadChunk[];
  readonly blockedAt: number | null;
  /** Event ID → `scope_seq` of the tail and the held chunks (one position per ID, else `event_conflict`). */
  readonly ids: ReadonlyMap<string, number>;
  readonly history: HistoryWindow | null;
}

export interface TerminalState {
  readonly sessions: Readonly<Record<string, TerminalEntry>>;
}

declare module '../store' {
  interface StoreState {
    terminal: TerminalState;
  }
}

export const slice: Slice<'terminal'> = { name: 'terminal', initial: () => ({ sessions: {} }) };

// --- store actions ---

/** Applies `update` to the session's entry (none: nothing). */
export function updateEntry(store: Store, session: string, update: (entry: TerminalEntry) => TerminalEntry): void {
  store.update('terminal', (state) => {
    const entry = state.sessions[session];
    if (entry === undefined) return state;
    const next = update(entry);
    return next === entry ? state : { sessions: { ...state.sessions, [session]: next } };
  });
}

/** A pull failed: the code is shown; what was applied stays. */
export function terminalFailed(store: Store, session: string, code: string): void {
  updateEntry(store, session, (entry) =>
    entry.status === 'error' && entry.code === code ? entry : { ...entry, status: 'error', code },
  );
}

/** Frames of the live channel this slice applies (`core/net/frames.ts`). */
export const frames: Readonly<Record<string, (store: Store, data: unknown) => void>> = {
  terminals_refused: (store, data) => {
    const sessions = isRecord(data) && Array.isArray(data.sessions) ? data.sessions : [];
    for (const session of sessions) {
      if (typeof session === 'string') terminalFailed(store, session, 'not_found');
    }
  },
};
