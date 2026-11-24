// Slice `terminal` (06-web.md sections 2.3 and 5.5, ADR-016): the terminal chunks of the watched sessions only (a
// session leaves the slice when its view closes). Per session:
//
// - `chunks`: verified chunks applied strictly in `scope_seq` order (the view writes them in this order);
// - `through`: every chunk of the session at or below this `scope_seq` is known (applied or unverifiable) – set by the
//   history pull (its last page's `head`) and moved by live chunks;
// - `held`: live chunks beyond a possible gap. Chunks of one session are a subsequence of the workgroup scope, so a
//   live chunk at `seq` is next only if every `scope_seq` between `through` and `seq` is accounted for, i.e. a verified
//   event that is not a terminal chunk of this session (`accounted`). Anything else – a missing seq (a lost frame, a
//   chunk of an unwatched session), a pending, locked or unverifiable event – may hide a chunk of this session, so the
//   held chunks wait and the watcher pulls (`core/terminal/watch.ts`) before anything further is rendered;
// - `bad`: chunks the reader refused or that break the contract (shown as a notice, never as output, never dropped);
// - `blockedAt`: a pulled chunk that could not be read yet (pending log, missing epoch key): nothing at or beyond it is
//   applied until a later pull reads it.
//
// The slice also handles the frame `terminals_refused {sessions}` of the live channel (the hub would not watch them).
import { parseTime } from '../../encoding/time';
import { type Chunk, chunkAt, parseChunk } from '../../terminal/chunks';
import { isRecord } from '../../registry';
import type { EventEntry, EventsState } from './events';
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

export interface TerminalEntry extends TerminalTarget {
  readonly status: TerminalStatus;
  readonly code: string | null;
  readonly chunks: readonly Chunk[];
  readonly through: number;
  readonly held: readonly Chunk[];
  readonly bad: readonly BadChunk[];
  readonly blockedAt: number | null;
  /** Event ID → `scope_seq` (one position per ID, else `event_conflict`). */
  readonly ids: ReadonlyMap<string, number>;
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

/** One read result of a chunk event: verified with its body, unverifiable with its code, or not readable yet. */
export type ChunkVerdict =
  | {
      readonly seq: number;
      readonly status: 'verified';
      readonly id: string;
      readonly body: unknown;
      /** The header time (`ts`). */
      readonly ts?: unknown;
    }
  | { readonly seq: number; readonly status: 'unverifiable'; readonly code: string }
  | { readonly seq: number; readonly status: 'waiting' };

/** The verdict of an `events` entry the reader settled (or not yet). */
export function verdictOf(entry: EventEntry): ChunkVerdict {
  if (entry.status === 'verified') {
    return {
      seq: entry.scopeSeq,
      status: 'verified',
      id: entry.header!.id as string,
      body: entry.body,
      ts: entry.header!.ts,
    };
  }
  if (entry.status === 'unverifiable') return { seq: entry.scopeSeq, status: 'unverifiable', code: entry.code! };
  return { seq: entry.scopeSeq, status: 'waiting' };
}

/**
 * The verdict of a pulled entry: the hub answered `?session=…&kinds=session.terminal`, but it is not trusted – a
 * verified event that is not a chunk of this session (another session's chunk, another kind) is `header_invalid`.
 */
export function pulledVerdict(entry: EventEntry, session: string): ChunkVerdict {
  if (entry.status === 'verified' && !isChunkOf(entry, session))
    return { seq: entry.scopeSeq, status: 'unverifiable', code: 'header_invalid' };
  return verdictOf(entry);
}

/** Whether a stored event is a terminal chunk of `session`. */
export const isChunkOf = (entry: EventEntry, session: string): boolean =>
  entry.status === 'verified' && entry.header!.kind === TERMINAL_KIND && entry.header!.session === session;

/** `accounted(seq)` of the module comment for the workgroup scope of `target` in `events`. */
export function accountedIn(events: EventsState, target: TerminalTarget): (seq: number) => boolean {
  const entries = events[target.wg]?.entries;
  return (seq) => {
    const entry = entries?.get(seq);
    return entry !== undefined && entry.status === 'verified' && !isChunkOf(entry, target.session);
  };
}

// --- pure steps ---

function fresh(target: TerminalTarget): TerminalEntry {
  return {
    ...target,
    status: 'loading',
    code: null,
    chunks: [],
    through: 0,
    held: [],
    bad: [],
    blockedAt: null,
    ids: new Map(),
  };
}

const known = (entry: TerminalEntry, seq: number): boolean =>
  seq <= entry.through || entry.held.some((chunk) => chunk.seq === seq);

/** Unix ms of a header time, null if it is none (a verified header always has one; older test data may not). */
function headerMs(ts: unknown): number | null {
  try {
    return parseTime(ts);
  } catch {
    return null;
  }
}

/** A verified verdict as a chunk, or the code that makes it bad (contract or a second position of its ID). */
function toChunk(entry: TerminalEntry, verdict: ChunkVerdict & { status: 'verified' }): Chunk | string {
  const seen = entry.ids.get(verdict.id);
  if (seen !== undefined && seen !== verdict.seq) return 'event_conflict';
  const body = parseChunk(verdict.body);
  return body === null ? 'header_invalid' : chunkAt(verdict.seq, body, headerMs(verdict.ts));
}

function withId(ids: ReadonlyMap<string, number>, id: string, seq: number): ReadonlyMap<string, number> {
  if (ids.get(id) === seq) return ids;
  return new Map(ids).set(id, seq);
}

/** Applies the held chunks that are next (module comment); stops at the first possible gap. */
export function release(entry: TerminalEntry, accounted: (seq: number) => boolean): TerminalEntry {
  const held = [...entry.held].sort((a, b) => a.seq - b.seq);
  const chunks = [...entry.chunks];
  let through = entry.through;
  while (held.length > 0) {
    const next = held[0]!;
    if (next.seq <= through) {
      held.shift();
      continue;
    }
    if (entry.blockedAt !== null && next.seq >= entry.blockedAt) break;
    let open = false;
    for (let seq = through + 1; seq < next.seq && !open; seq += 1) open = !accounted(seq);
    if (open) break;
    held.shift();
    chunks.push(next);
    through = next.seq;
  }
  if (through === entry.through && held.length === entry.held.length) return entry;
  return { ...entry, chunks, held, through };
}

/** A live chunk event (from the `events` slice) of the session. */
export function receive(entry: TerminalEntry, verdict: ChunkVerdict): TerminalEntry {
  if (verdict.status !== 'verified' || known(entry, verdict.seq)) return entry;
  const chunk = toChunk(entry, verdict);
  // a broken live chunk waits for the pull, which records it as bad at its position
  if (typeof chunk === 'string') return entry;
  return { ...entry, held: [...entry.held, chunk], ids: withId(entry.ids, verdict.id, verdict.seq) };
}

/**
 * One pulled page (`GET …/events?session=…&kinds=session.terminal`, ascending): every chunk of the session up to
 * `upTo` (`next_after` of a further page, else the scope `head`) is in it. Chunks are applied in order; a chunk that
 * cannot be read yet blocks there (`blockedAt`), so `through` stays below it.
 */
export function applyPage(entry: TerminalEntry, verdicts: readonly ChunkVerdict[], upTo: number): TerminalEntry {
  let chunks = [...entry.chunks];
  let bad = [...entry.bad];
  let ids = entry.ids;
  let held = entry.held;
  let through = entry.through;
  let blockedAt: number | null = null;
  for (const verdict of [...verdicts].sort((a, b) => a.seq - b.seq)) {
    if (verdict.seq <= through) continue;
    if (blockedAt !== null) {
      if (verdict.status === 'verified') {
        const chunk = toChunk({ ...entry, ids }, verdict);
        if (typeof chunk !== 'string' && !held.some((item) => item.seq === chunk.seq)) held = [...held, chunk];
      }
      continue;
    }
    if (verdict.status === 'waiting') {
      blockedAt = verdict.seq;
      through = verdict.seq - 1;
      continue;
    }
    const result = verdict.status === 'verified' ? toChunk({ ...entry, ids }, verdict) : verdict.code;
    if (typeof result === 'string') bad = [...bad, { seq: verdict.seq, code: result }];
    else {
      chunks = [...chunks, result];
      ids = withId(ids, (verdict as { id: string }).id, verdict.seq);
    }
    through = verdict.seq;
  }
  if (blockedAt === null) through = Math.max(through, upTo);
  held = held.filter((chunk) => chunk.seq > through);
  return { ...entry, chunks, bad, ids, held, through, blockedAt, status: 'ready', code: null };
}

// --- store actions ---

function updateEntry(store: Store, session: string, update: (entry: TerminalEntry) => TerminalEntry): void {
  store.update('terminal', (state) => {
    const entry = state.sessions[session];
    if (entry === undefined) return state;
    const next = update(entry);
    return next === entry ? state : { sessions: { ...state.sessions, [session]: next } };
  });
}

/** A session's terminal is watched: a fresh, loading entry (an existing one is kept). */
export function openTerminal(store: Store, target: TerminalTarget): void {
  store.update('terminal', (state) =>
    state.sessions[target.session] !== undefined
      ? state
      : { sessions: { ...state.sessions, [target.session]: fresh(target) } },
  );
}

/** The view closed: the session's chunks leave memory (06-web.md section 2.3). */
export function closeTerminal(store: Store, session: string): void {
  store.update('terminal', (state) => {
    if (state.sessions[session] === undefined) return state;
    const { [session]: _closed, ...rest } = state.sessions;
    return { sessions: rest };
  });
}

/** Live chunks of the session, then whatever is next. */
export function receiveChunks(
  store: Store,
  session: string,
  verdicts: readonly ChunkVerdict[],
  accounted: (seq: number) => boolean,
): void {
  updateEntry(store, session, (entry) => release(verdicts.reduce(receive, entry), accounted));
}

/** A pulled page of the session, then whatever is next. */
export function pulledPage(
  store: Store,
  session: string,
  verdicts: readonly ChunkVerdict[],
  upTo: number,
  accounted: (seq: number) => boolean,
): void {
  updateEntry(store, session, (entry) => release(applyPage(entry, verdicts, upTo), accounted));
}

/** Re-checks the held chunks (the `events` slice changed). */
export function releaseHeld(store: Store, session: string, accounted: (seq: number) => boolean): void {
  updateEntry(store, session, (entry) => release(entry, accounted));
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
