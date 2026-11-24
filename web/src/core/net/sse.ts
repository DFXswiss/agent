// The browser live channel (03-api.md section 5, 06-web.md sections 1.3 and 12.5): `GET /v1/stream` read with `fetch`
// streaming. Push is a hint, pull is the truth: frames are de-duplicated by `(scope_id, scope_seq)` against the
// cursors, a frame above `cursor + 1` makes the channel pull the gap with `GET /v1/scopes/{id}/events`, `hello`
// pulls every scope whose head is beyond its cursor and `resync` pulls every cursor; only then is the channel "live"
// (and the frame is handed on, so that slices reload what a lost row frame may have changed).
// A missing heartbeat (the hub sends a comment every 15 s), an ended stream or a failed pull reconnect with backoff
// 1 s → 30 s and jitter. Frames are handled strictly in order, one at a time.
//
// Watched terminals (`terminal=`, at most `MAX_TERMINALS`) change with `setTerminals`: a running connection is reopened
// with the new list (the cursors stay in memory, so the hub replays only what the client lacks). The hub refuses the
// whole stream (`404 not_found`) when a watched session is not readable; the channel then drops the newest watched
// session, reports it with the frame `terminals_refused {sessions}` and connects again at once (one by one, so a
// refused new session does not end the others; with no terminal left the refusal is fatal as before).
// After `resync` (the hub dropped frames, ADR-063 section 2) the channel pulls only `resyncKinds` (the non-bulk kinds:
// titles, whiteboards, profiles, prompts, operations) from each cursor up to the head, without skipping ahead, and
// moves the cursor to the head; the `resync` frame then makes the mounted views read their newest page
// (`core/store/focus.ts`), so a flood of session output costs one page per open view instead of a pull of everything.
// The `monitoring=1` flag (`metrics` frames, only while a monitoring screen is open) changes with `setMonitoring` the
// same way: a running connection is reopened.
import { HubError, type SignedClient } from './signedFetch';
import { Cursors } from './cursors';

/** Backoff bounds of the reconnect (06-web.md section 12.5). */
export const BACKOFF_MIN_MS = 1000;
export const BACKOFF_MAX_MS = 30_000;
/** Three missed heartbeats of 15 s end the connection. */
export const HEARTBEAT_TIMEOUT_MS = 45_000;
/** Page size of gap pulls (maximum of `GET /v1/scopes/{id}/events`). */
export const PAGE_LIMIT = 1000;
/** A scope further behind than this skips ahead (the store keeps at most 5 000 events per scope). */
export const MAX_BACKFILL = 5000;
/** A gap is repaired by a pull at most this long after the frame that revealed it. */
export const REPAIR_DELAY_MS = 1000;
/** Watched terminal sessions per stream (03-api.md section 5). */
export const MAX_TERMINALS = 3;
/** Errors after which reconnecting cannot help (the UI asks for a new sign-in or leaves the org). */
export const FATAL_CODES: ReadonlySet<string> = new Set([
  'session_expired',
  'unauthorized',
  'key_unknown',
  'key_revoked',
  'forbidden',
  'not_found',
]);

// --- frame parser ---

export type SseItem =
  { readonly kind: 'frame'; readonly event: string; readonly data: string } | { readonly kind: 'comment' };

/**
 * Incremental `text/event-stream` parser (the subset the hub sends: `event:`, `data:`, comments; `id:`/`retry:` and
 * unknown fields are ignored). Accepts text in arbitrary pieces; lines end with LF, CRLF or CR.
 */
export class SseParser {
  #buffer = '';
  #event = '';
  #data: string[] = [];
  #pendingCr = false;

  push(text: string): SseItem[] {
    const items: SseItem[] = [];
    let input = text;
    if (this.#pendingCr && input.startsWith('\n')) input = input.slice(1);
    this.#pendingCr = false;
    this.#buffer += input;
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.#buffer);
      if (match === null) break;
      // a CR at the very end may be the first half of CRLF
      if (match[0] === '\r' && match.index === this.#buffer.length - 1) this.#pendingCr = true;
      const line = this.#buffer.slice(0, match.index);
      this.#buffer = this.#buffer.slice(match.index + match[0].length);
      const item = this.#line(line);
      if (item !== null) items.push(item);
    }
    return items;
  }

  #line(line: string): SseItem | null {
    if (line === '') {
      if (this.#data.length === 0) {
        this.#event = '';
        return null;
      }
      const frame: SseItem = {
        kind: 'frame',
        event: this.#event === '' ? 'message' : this.#event,
        data: this.#data.join('\n'),
      };
      this.#event = '';
      this.#data = [];
      return frame;
    }
    if (line.startsWith(':')) return { kind: 'comment' };
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.#event = value;
    else if (field === 'data') this.#data.push(value);
    return null;
  }
}

// --- channel ---

export type LiveStatus = 'idle' | 'connecting' | 'syncing' | 'live' | 'reconnecting' | 'offline' | 'stopped';

export interface LiveState {
  readonly status: LiveStatus;
  /** Unix ms of the next attempt while `reconnecting`. */
  readonly retryAt: number | null;
  /** Failed attempts since the channel was last live. */
  readonly attempt: number;
  /** Code of the last failure (`HubError.code`), null after reaching "live". */
  readonly error: string | null;
}

/** An event as the hub stores it (`stored_event`); stream frames carry no `received_at`. */
export interface StoredEvent {
  readonly scope_seq: number;
  readonly received_at: string | null;
  readonly event: unknown;
}

export interface LiveHandlers {
  /** New events of a scope in ascending order (frames and pulls, never duplicates). */
  readonly events: (scopeId: string, items: StoredEvent[]) => void | Promise<void>;
  /** Every other frame (`task`, `session`, `operation`, `log`, `wraps`, `notification`, …) with parsed data, and
   *  `hello` after the channel processed it. */
  readonly frame: (type: string, data: unknown) => void | Promise<void>;
  readonly state: (state: LiveState) => void;
}

export interface Timers {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface LiveOptions {
  readonly client: Pick<SignedClient, 'request' | 'stream'>;
  readonly org: string;
  readonly cursors: Cursors;
  readonly handlers: LiveHandlers;
  readonly terminals?: readonly string[];
  readonly monitoring?: boolean;
  /** The kinds pulled after `resync` (default: every kind, as after `hello`). */
  readonly resyncKinds?: readonly string[];
  readonly timers?: Timers;
  readonly random?: () => number;
  readonly now?: () => number;
}

export const SYSTEM_TIMERS: Timers = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>),
};

/** Delay before reconnect attempt `attempt` (1-based): doubling from 1 s, ±20 % jitter, within 1 s … 30 s. */
export function backoff(attempt: number, random: number): number {
  const base = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.min(attempt - 1, 16));
  const jittered = Math.round(base * (0.8 + 0.4 * random));
  return Math.min(BACKOFF_MAX_MS, Math.max(BACKOFF_MIN_MS, jittered));
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

function parseData(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

/** `{scope_id, scope_seq, event}` of an `event`/`terminal` frame; null if malformed. */
function eventData(data: unknown): { scopeId: string; seq: number; event: unknown } | null {
  if (!isRecord(data) || typeof data.scope_id !== 'string' || !isSeq(data.scope_seq) || data.scope_seq < 1) return null;
  if (!isRecord(data.event)) return null;
  return { scopeId: data.scope_id, seq: data.scope_seq, event: data.event };
}

interface EventsPage {
  readonly events: StoredEvent[];
  readonly next_after: number | null;
  readonly head: number;
}

function isPage(value: unknown): value is EventsPage {
  return (
    isRecord(value) &&
    Array.isArray(value.events) &&
    value.events.every((item) => isRecord(item) && isSeq(item.scope_seq) && isRecord(item.event)) &&
    (value.next_after === null || isSeq(value.next_after)) &&
    isSeq(value.head)
  );
}

class Superseded extends Error {}

/** One live channel for one org. */
export class LiveChannel {
  readonly #options: LiveOptions;
  readonly #timers: Timers;
  readonly #random: () => number;
  readonly #now: () => number;
  #generation = 0;
  #abort: AbortController | null = null;
  #retry: unknown = null;
  #heartbeat: unknown = null;
  #queue: Promise<void> = Promise.resolve();
  #state: LiveState = { status: 'idle', retryAt: null, attempt: 0, error: null };
  #offline = false;
  /** Seqs delivered beyond a gap, per scope (the cursor stays at the last contiguous seq until the repair). */
  readonly #ahead = new Map<string, Set<number>>();
  readonly #repairs = new Map<string, unknown>();
  #terminals: readonly string[];
  #monitoring: boolean;

  constructor(options: LiveOptions) {
    this.#options = options;
    this.#monitoring = options.monitoring === true;
    this.#terminals = (options.terminals ?? []).slice(-MAX_TERMINALS);
    this.#timers = options.timers ?? SYSTEM_TIMERS;
    this.#random = options.random ?? Math.random;
    this.#now = options.now ?? Date.now;
  }

  get state(): LiveState {
    return this.#state;
  }

  #set(patch: Partial<LiveState>): void {
    this.#state = { ...this.#state, ...patch };
    this.#options.handlers.state(this.#state);
  }

  /** Connects (no effect while running). */
  start(): void {
    if (this.#abort !== null || this.#retry !== null || this.#offline) return;
    this.#open();
  }

  /** The watched terminal sessions (the newest `MAX_TERMINALS`); a running connection is reopened with them. */
  setTerminals(sessions: readonly string[]): void {
    const next = [...new Set(sessions)].slice(-MAX_TERMINALS);
    if (next.length === this.#terminals.length && next.every((session, index) => session === this.#terminals[index]))
      return;
    this.#terminals = next;
    if (this.#abort !== null) this.#open();
  }

  get terminals(): readonly string[] {
    return this.#terminals;
  }

  /** Whether `metrics` frames are requested; a running connection is reopened when this changes. */
  setMonitoring(on: boolean): void {
    if (on === this.#monitoring) return;
    this.#monitoring = on;
    if (this.#abort !== null) this.#open();
  }

  get monitoring(): boolean {
    return this.#monitoring;
  }

  /** Closes the connection and cancels every timer (also leaves the offline state: `online()` does not restart). */
  stop(): void {
    this.#offline = false;
    this.#close();
    this.#set({ status: 'stopped', retryAt: null });
  }

  /** `navigator.onLine` became false: close and wait for `online()`. */
  offline(): void {
    this.#offline = true;
    this.#close();
    this.#set({ status: 'offline', retryAt: null });
  }

  /** Back online: connect at once (only if `offline()` closed the channel). */
  online(): void {
    if (!this.#offline) return;
    this.#offline = false;
    this.#open();
  }

  #close(): void {
    this.#generation += 1;
    this.#abort?.abort();
    this.#abort = null;
    this.#cancel('#retry');
    this.#cancel('#heartbeat');
    for (const scope of [...this.#repairs.keys()]) this.#cancelRepair(scope);
  }

  #cancel(which: '#retry' | '#heartbeat'): void {
    const handle = which === '#retry' ? this.#retry : this.#heartbeat;
    if (handle !== null) this.#timers.clearTimeout(handle);
    if (which === '#retry') this.#retry = null;
    else this.#heartbeat = null;
  }

  #target(): string {
    const query = new URLSearchParams({ org: this.#options.org });
    for (const session of this.#terminals) query.append('terminal', session);
    const cursors = this.#options.cursors.query();
    if (cursors !== '') query.set('cursors', cursors);
    if (this.#monitoring) query.set('monitoring', '1');
    return `/v1/stream?${query.toString()}`;
  }

  #open(): void {
    this.#close();
    const generation = this.#generation;
    const abort = new AbortController();
    this.#abort = abort;
    this.#queue = Promise.resolve();
    this.#set({ status: 'connecting', retryAt: null });
    void this.#run(generation, abort);
  }

  async #run(generation: number, abort: AbortController): Promise<void> {
    try {
      const response = await this.#options.client.stream(this.#target(), abort.signal);
      await this.#read(response, generation, abort);
      throw new HubError('unavailable', 0);
    } catch (error) {
      this.#failed(generation, error);
    }
  }

  #failed(generation: number, error: unknown): void {
    if (generation !== this.#generation) return;
    const code = error instanceof HubError ? error.code : 'unavailable';
    if (code === 'not_found' && this.#terminals.length > 0 && this.#state.status === 'connecting') {
      // the newest watched session is the likely cause (the others were watched before): drop it and try again; if the
      // stream is refused for another reason, every terminal is dropped in turn and then the refusal is fatal
      const refused = this.#terminals.at(-1)!;
      this.#terminals = this.#terminals.slice(0, -1);
      this.#open();
      void this.#options.handlers.frame('terminals_refused', { sessions: [refused] });
      return;
    }
    if (FATAL_CODES.has(code)) {
      this.#close();
      this.#set({ status: 'stopped', retryAt: null, error: code });
      return;
    }
    this.#close();
    const attempt = this.#state.attempt + 1;
    const delay = backoff(attempt, this.#random());
    this.#retry = this.#timers.setTimeout(() => {
      this.#retry = null;
      this.#open();
    }, delay);
    this.#set({ status: 'reconnecting', retryAt: this.#now() + delay, attempt, error: code });
  }

  #armHeartbeat(abort: AbortController): void {
    this.#cancel('#heartbeat');
    this.#heartbeat = this.#timers.setTimeout(() => {
      this.#heartbeat = null;
      abort.abort();
    }, HEARTBEAT_TIMEOUT_MS);
  }

  async #read(response: Response, generation: number, abort: AbortController): Promise<void> {
    if (response.body === null) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    for (;;) {
      this.#armHeartbeat(abort);
      const { value, done } = await reader.read();
      if (done) break;
      for (const item of parser.push(decoder.decode(value, { stream: true }))) this.#enqueue(item, generation);
    }
    // frames that arrived before the end are still handled before the reconnect
    await this.#queue;
  }

  #enqueue(item: SseItem, generation: number): void {
    if (item.kind === 'comment') return;
    this.#task(generation, (signal) => this.#handle(item.event, parseData(item.data), generation, signal));
  }

  /** Runs `work` after everything queued before it; a failure closes the connection and reconnects. */
  #task(generation: number, work: (signal: AbortSignal) => Promise<void>): void {
    const abort = this.#abort!;
    this.#queue = this.#queue.then(async () => {
      if (generation !== this.#generation) return;
      try {
        await work(abort.signal);
      } catch (error) {
        if (error instanceof Superseded) return;
        abort.abort();
        this.#failed(generation, error);
      }
    });
  }

  #check(generation: number): void {
    if (generation !== this.#generation) throw new Superseded();
  }

  async #handle(type: string, data: unknown, generation: number, signal: AbortSignal): Promise<void> {
    if (data === undefined) return;
    if (type === 'hello') return this.#hello(data, generation, signal);
    if (type === 'resync') return this.#resync(data, generation, signal);
    if (type === 'event' || type === 'terminal') return this.#event(data, generation);
    await this.#options.handlers.frame(type, data);
  }

  async #hello(data: unknown, generation: number, signal: AbortSignal): Promise<void> {
    this.#set({ status: 'syncing' });
    const heads = isRecord(data) && isRecord(data.heads) ? data.heads : {};
    const cursors = this.#options.cursors;
    for (const scope of Object.keys(heads).sort()) {
      const head = heads[scope];
      if (!isSeq(head)) continue;
      const cursor = cursors.get(scope);
      if (cursor === undefined || head < cursor) {
        // a new scope starts at its head; a head below the cursor means the hub was restored: start over there
        cursors.reset(scope, head);
        this.#ahead.delete(scope);
      } else if (head > cursor) {
        await this.#pull(scope, generation, signal);
      }
    }
    this.#live();
    // the hub clock of `hello.hub_time` is for the slices (countdown elapsed times, 06-web.md section 4.4)
    await this.#options.handlers.frame('hello', data);
  }

  async #resync(data: unknown, generation: number, signal: AbortSignal): Promise<void> {
    this.#set({ status: 'syncing' });
    const kinds = this.#options.resyncKinds;
    for (const scope of this.#options.cursors.scopes()) await this.#pull(scope, generation, signal, kinds);
    this.#live();
    // the hub may have dropped row frames (`task`, `operation`, …) as well: the slices reload like after `hello`
    await this.#options.handlers.frame('resync', data);
  }

  #live(): void {
    this.#set({ status: 'live', retryAt: null, attempt: 0, error: null });
  }

  /** Whether the client already holds `seq` of `scope` (at or below the cursor, or delivered ahead of a gap). */
  #held(scope: string, seq: number): boolean {
    return seq <= (this.#options.cursors.get(scope) ?? 0) || this.#ahead.get(scope)?.has(seq) === true;
  }

  async #event(data: unknown, generation: number): Promise<void> {
    const found = eventData(data);
    if (found === null || this.#held(found.scopeId, found.seq)) return;
    const gap = found.seq > (this.#options.cursors.get(found.scopeId) ?? 0) + 1;
    await this.#accept(found.scopeId, [{ scope_seq: found.seq, received_at: null, event: found.event }], generation);
    if (gap) this.#scheduleRepair(found.scopeId, generation);
  }

  /** Hands new events to the handler, then records them and moves the cursor over every contiguous seq. */
  async #accept(scope: string, items: StoredEvent[], generation: number): Promise<void> {
    this.#check(generation);
    const fresh = items.filter((item) => !this.#held(scope, item.scope_seq));
    if (fresh.length === 0) return;
    await this.#options.handlers.events(scope, fresh);
    this.#check(generation);
    const ahead = this.#ahead.get(scope) ?? new Set<number>();
    for (const item of fresh) ahead.add(item.scope_seq);
    const cursors = this.#options.cursors;
    let cursor = cursors.get(scope) ?? 0;
    while (ahead.delete(cursor + 1)) cursor += 1;
    cursors.advance(scope, cursor);
    if (ahead.size === 0) this.#ahead.delete(scope);
    else this.#ahead.set(scope, ahead);
  }

  /**
   * A gap is repaired by one pull per scope at most every `REPAIR_DELAY_MS`: unwatched `session.terminal` chunks
   * leave gaps in the event seqs, and pulling for every following frame would exhaust the rate limit.
   */
  #scheduleRepair(scope: string, generation: number): void {
    if (this.#repairs.has(scope)) return;
    const handle = this.#timers.setTimeout(() => {
      // `#close` cancels every repair, so the generation is still current here
      this.#repairs.delete(scope);
      this.#task(generation, (signal) => this.#pull(scope, generation, signal));
    }, REPAIR_DELAY_MS);
    this.#repairs.set(scope, handle);
  }

  /**
   * Pulls a scope from its cursor up to the hub's head (skipping ahead beyond `MAX_BACKFILL`); with `kinds` only these
   * kinds, without skipping ahead (`resync`, module comment).
   */
  async #pull(scope: string, generation: number, signal: AbortSignal, kinds?: readonly string[]): Promise<void> {
    const cursors = this.#options.cursors;
    // every pulled scope has a cursor: `hello`/`resync` pull known scopes, a gap frame set one in `#accept`
    let after = cursors.get(scope)!;
    let first = kinds === undefined;
    const filter = kinds === undefined ? '' : `&kinds=${kinds.join(',')}`;
    for (;;) {
      let page: unknown;
      try {
        page = await this.#options.client.request(
          'GET',
          `/v1/scopes/${encodeURIComponent(scope)}/events?after=${after}&limit=${PAGE_LIMIT}${filter}`,
          { signal },
        );
      } catch (error) {
        if (error instanceof HubError && error.code === 'not_found') {
          cursors.forget(scope);
          this.#ahead.delete(scope);
          this.#cancelRepair(scope);
          return;
        }
        throw error;
      }
      this.#check(generation);
      if (!isPage(page)) throw new HubError('unavailable', 200);
      if (first && page.head - after > MAX_BACKFILL) {
        after = page.head - MAX_BACKFILL;
        first = false;
        continue;
      }
      first = false;
      const head = page.head;
      const items = page.events
        .filter((item) => item.scope_seq > after && item.scope_seq <= head)
        .sort((a, b) => a.scope_seq - b.scope_seq)
        .map((item) => ({ scope_seq: item.scope_seq, received_at: item.received_at ?? null, event: item.event }));
      await this.#accept(scope, items, generation);
      if (page.next_after === null) {
        // everything up to the head is held now (seqs the pull does not return are terminal chunks of others)
        cursors.advance(scope, head);
        this.#cancelRepair(scope);
        const ahead = this.#ahead.get(scope);
        if (ahead === undefined) return;
        for (const seq of ahead) if (seq <= head) ahead.delete(seq);
        // frames beyond a stale head leave a gap: repair again later
        if (ahead.size === 0) this.#ahead.delete(scope);
        else this.#scheduleRepair(scope, generation);
        return;
      }
      after = Math.max(after, page.next_after);
    }
  }

  #cancelRepair(scope: string): void {
    const handle = this.#repairs.get(scope);
    if (handle === undefined) return;
    this.#timers.clearTimeout(handle);
    this.#repairs.delete(scope);
  }
}
