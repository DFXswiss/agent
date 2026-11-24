// The steps and store actions on the terminal entries of the watched sessions (the semantics: the module comment of
// `core/store/slices/terminal.ts`). Used by the terminal view's watcher (`core/terminal/watch.ts`) only, so this code
// is loaded with the view.
import { parseTime } from '../encoding/time';
import type { EventEntry, EventsState } from '../store/slices/events';
import {
  type BadChunk,
  HISTORY_CHARS,
  type HistoryWindow,
  KEEP_CHARS,
  TERMINAL_KIND,
  type TerminalEntry,
  type TerminalTarget,
  updateEntry,
} from '../store/slices/terminal';
import type { Store } from '../store/store';
import { type Chunk, chunkAt, parseChunk } from './chunks';

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
