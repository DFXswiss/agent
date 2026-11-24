// Slice `terminal` (06-web.md sections 2.3 and 5.5, ADR-016, ADR-063 section 1): the terminal chunks of the watched
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

/** Characters of output the tail keeps (ADR-063 section 1: 8 MiB of output in Follow mode). */
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

type Draft = { -readonly [K in keyof TerminalEntry]: TerminalEntry[K] };

function fresh(target: TerminalTarget): TerminalEntry {
  return {
    ...target,
    status: 'loading',
    code: null,
    chunks: [],
    chars: 0,
    earlier: null,
    epoch: 0,
    through: 0,
    held: [],
    bad: [],
    blockedAt: null,
    ids: new Map(),
    history: null,
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

type Verified = ChunkVerdict & { status: 'verified' };

/** A verified verdict as a chunk, or the code that makes it bad (contract or a second position of its ID). */
function toChunk(ids: ReadonlyMap<string, number>, verdict: Verified): Chunk | string {
  const seen = ids.get(verdict.id);
  if (seen !== undefined && seen !== verdict.seq) return 'event_conflict';
  const body = parseChunk(verdict.body);
  return body === null ? 'header_invalid' : chunkAt(verdict.seq, body, headerMs(verdict.ts), verdict.id);
}

const owned = <T>(items: readonly T[]): T[] => items as T[];
const ownedIds = (ids: ReadonlyMap<string, number>): Map<string, number> => ids as Map<string, number>;

/** Appends an applied chunk to the tail and, while it is open at the end, to the history window. */
function append(next: Draft, chunk: Chunk): void {
  owned(next.chunks).push(chunk);
  next.chars += chunk.chars;
  ownedIds(next.ids).set(chunk.id, chunk.seq);
  const window = next.history;
  if (window === null || window.cut) return;
  if (window.chars + chunk.chars > HISTORY_CHARS) {
    next.history = { ...window, cut: true };
    return;
  }
  owned(window.chunks).push(chunk);
  ownedIds(window.ids).set(chunk.id, chunk.seq);
  next.history = { ...window, chars: window.chars + chunk.chars };
}

/** Records a bad chunk in the tail and, while it is open at the end, in the history window. */
function addBad(next: Draft, item: BadChunk): void {
  owned(next.bad).push(item);
  const window = next.history;
  if (window !== null && !window.cut) next.history = { ...window, bad: [...window.bad, item] };
}

/**
 * Keeps the newest `limit` characters of the tail: beyond it the oldest chunks leave memory until an eighth is free
 * (the newest chunk always stays); `earlier` then points at the first chunk in memory.
 */
export function trimTail(entry: TerminalEntry, limit: number = KEEP_CHARS): TerminalEntry {
  if (entry.chars <= limit) return entry;
  const chunks = owned(entry.chunks);
  const goal = limit - limit / 8;
  let chars = entry.chars;
  let drop = 0;
  while (chars > goal && drop < chunks.length - 1) {
    chars -= chunks[drop]!.chars;
    drop += 1;
  }
  const ids = ownedIds(entry.ids);
  // an ID has one position (a second one is `event_conflict`), so the IDs of dropped chunks are simply forgotten
  for (const chunk of chunks.splice(0, drop)) ids.delete(chunk.id);
  const first = chunks[0]!.seq;
  return { ...entry, chars, earlier: first, bad: entry.bad.filter((item) => item.seq > first) };
}

/** Applies the held chunks that are next (module comment); stops at the first possible gap. */
export function release(entry: TerminalEntry, accounted: (seq: number) => boolean): TerminalEntry {
  const held = [...entry.held].sort((a, b) => a.seq - b.seq);
  const next: Draft = { ...entry };
  while (held.length > 0) {
    const chunk = held[0]!;
    if (chunk.seq <= next.through) {
      held.shift();
      continue;
    }
    if (entry.blockedAt !== null && chunk.seq >= entry.blockedAt) break;
    let open = false;
    for (let seq = next.through + 1; seq < chunk.seq && !open; seq += 1) open = !accounted(seq);
    if (open) break;
    held.shift();
    append(next, chunk);
    next.through = chunk.seq;
  }
  if (next.through === entry.through && held.length === entry.held.length) return entry;
  next.held = held;
  return trimTail(next);
}

/** A live chunk event (from the `events` slice) of the session. */
export function receive(entry: TerminalEntry, verdict: ChunkVerdict): TerminalEntry {
  if (verdict.status !== 'verified' || known(entry, verdict.seq)) return entry;
  const chunk = toChunk(entry.ids, verdict);
  // a broken live chunk waits for the pull, which records it as bad at its position
  if (typeof chunk === 'string') return entry;
  ownedIds(entry.ids).set(verdict.id, verdict.seq);
  return { ...entry, held: [...entry.held, chunk] };
}

/**
 * One pulled page (`GET …/events?session=…&kinds=session.terminal`, ascending): every chunk of the session up to
 * `upTo` (`next_after` of a further page, else the scope `head`) is in it. Chunks are applied in order; a chunk that
 * cannot be read yet blocks there (`blockedAt`), so `through` stays below it.
 */
export function applyPage(entry: TerminalEntry, verdicts: readonly ChunkVerdict[], upTo: number): TerminalEntry {
  const next: Draft = { ...entry, blockedAt: null, status: 'ready', code: null };
  let held = entry.held;
  for (const verdict of [...verdicts].sort((a, b) => a.seq - b.seq)) {
    if (verdict.seq <= next.through) continue;
    if (next.blockedAt !== null) {
      if (verdict.status === 'verified') {
        const chunk = toChunk(next.ids, verdict);
        if (typeof chunk !== 'string' && !held.some((item) => item.seq === chunk.seq)) held = [...held, chunk];
      }
      continue;
    }
    if (verdict.status === 'waiting') {
      next.blockedAt = verdict.seq;
      next.through = verdict.seq - 1;
      continue;
    }
    const result = verdict.status === 'verified' ? toChunk(next.ids, verdict) : verdict.code;
    if (typeof result === 'string') addBad(next, { seq: verdict.seq, code: result });
    else append(next, result);
    next.through = verdict.seq;
  }
  if (next.blockedAt === null) next.through = Math.max(next.through, upTo);
  const through = next.through;
  next.held = held.filter((chunk) => chunk.seq > through);
  return trimTail(next);
}

/**
 * The newest page of the session (newest first from the hub, `eventsBefore`): it replaces the tail – the output
 * before it is not in memory (`earlier` = `nextBefore`). A tail that had content counts a new `epoch`, and the end of
 * an open history window is cut there.
 */
export function applyNewest(
  entry: TerminalEntry,
  verdicts: readonly ChunkVerdict[],
  upTo: number,
  nextBefore: number | null,
): TerminalEntry {
  const replaced = entry.chunks.length > 0 || entry.bad.length > 0;
  const ids = new Map<string, number>();
  for (const chunk of entry.held) ids.set(chunk.id, chunk.seq);
  const base: TerminalEntry = {
    ...entry,
    chunks: [],
    chars: 0,
    bad: [],
    ids,
    through: 0,
    blockedAt: null,
    earlier: nextBefore,
    epoch: replaced ? entry.epoch + 1 : entry.epoch,
    history: entry.history === null || !replaced ? entry.history : { ...entry.history, cut: true },
  };
  return applyPage(base, verdicts, upTo);
}

/** History mode: the window starts as a copy of the tail (once per change of mode). */
export function openHistory(entry: TerminalEntry): TerminalEntry {
  if (entry.history !== null) return entry;
  return {
    ...entry,
    history: {
      chunks: [...entry.chunks],
      chars: entry.chars,
      before: entry.earlier,
      cut: false,
      bad: [...entry.bad],
      status: 'ready',
      code: null,
      ids: new Map(entry.ids),
    },
  };
}

/** Back to Follow: the window leaves memory. */
export function closeHistory(entry: TerminalEntry): TerminalEntry {
  return entry.history === null ? entry : { ...entry, history: null };
}

/** The window loads the page before `before` (ignored unless that is still its cursor). */
export function earlierLoading(entry: TerminalEntry, before: number): TerminalEntry {
  const window = entry.history;
  if (window === null || window.before !== before || window.status === 'loading') return entry;
  return { ...entry, history: { ...window, status: 'loading', code: null } };
}

/** Loading the page before `before` failed (ignored unless that is still the window's cursor). */
export function earlierFailed(entry: TerminalEntry, before: number, code: string): TerminalEntry {
  const window = entry.history;
  if (window === null || window.before !== before) return entry;
  return { ...entry, history: { ...window, status: 'error', code } };
}

/**
 * The page before `before` (ascending, every chunk of the session in it down to `nextBefore`): put in front of the
 * window. A chunk that cannot be read yet fails the load (nothing is applied, it can be tried again). A window beyond
 * `limit` characters drops its newest chunks and is `cut` (the page itself always stays).
 */
export function applyEarlier(
  entry: TerminalEntry,
  before: number,
  verdicts: readonly ChunkVerdict[],
  nextBefore: number | null,
  limit: number = HISTORY_CHARS,
): TerminalEntry {
  const window = entry.history;
  if (window === null || window.before !== before) return entry;
  if (verdicts.some((verdict) => verdict.status === 'waiting')) return earlierFailed(entry, before, 'unavailable');
  const ids = ownedIds(window.ids);
  const page: Chunk[] = [];
  const bad: BadChunk[] = [];
  let chars = window.chars;
  for (const verdict of [...verdicts].sort((a, b) => a.seq - b.seq)) {
    if (verdict.seq >= before) continue;
    const result = verdict.status === 'verified' ? toChunk(ids, verdict) : (verdict as { code: string }).code;
    if (typeof result === 'string') bad.push({ seq: verdict.seq, code: result });
    else {
      page.push(result);
      ids.set(result.id, result.seq);
      chars += result.chars;
    }
  }
  const chunks = [...page, ...window.chunks];
  let cut = window.cut;
  while (chars > limit && chunks.length > page.length) {
    const dropped = chunks.pop()!;
    chars -= dropped.chars;
    ids.delete(dropped.id);
    cut = true;
  }
  const history: HistoryWindow = {
    chunks,
    chars,
    before: nextBefore,
    cut,
    bad: [...bad, ...window.bad],
    status: 'ready',
    code: null,
    ids,
  };
  return { ...entry, history };
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

/** The newest page of the session (the tail starts anew), then whatever is next. */
export function newestPage(
  store: Store,
  session: string,
  verdicts: readonly ChunkVerdict[],
  upTo: number,
  nextBefore: number | null,
  accounted: (seq: number) => boolean,
): void {
  updateEntry(store, session, (entry) => release(applyNewest(entry, verdicts, upTo, nextBefore), accounted));
}

/** History mode on (`openHistory`) or off (`closeHistory`). */
export function setHistory(store: Store, session: string, on: boolean): void {
  updateEntry(store, session, on ? openHistory : closeHistory);
}

/** An earlier page of the history window is loading. */
export function loadingEarlier(store: Store, session: string, before: number): void {
  updateEntry(store, session, (entry) => earlierLoading(entry, before));
}

/** An earlier page of the history window arrived. */
export function earlierPage(
  store: Store,
  session: string,
  before: number,
  verdicts: readonly ChunkVerdict[],
  nextBefore: number | null,
): void {
  updateEntry(store, session, (entry) => applyEarlier(entry, before, verdicts, nextBefore));
}

/** An earlier page of the history window could not be loaded. */
export function earlierPageFailed(store: Store, session: string, before: number, code: string): void {
  updateEntry(store, session, (entry) => earlierFailed(entry, before, code));
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
