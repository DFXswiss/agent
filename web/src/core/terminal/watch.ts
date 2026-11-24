// Watching one session's terminal (06-web.md section 5.5, 03-api.md sections 4.4 and 5):
//
// 1. the session joins the live channel's `terminal=` list (`TerminalContext.watch`, at most 3 per stream), so its
//    chunks arrive as `terminal` frames, pass the event reader and land in the `events` slice;
// 2. the history is pulled page by page with `GET /v1/scopes/{wg}/events?session=…&kinds=session.terminal` and read
//    with the reader *without* entering the `events` slice (a long history would push the chat out of its LRU);
// 3. every change of the `events` slice hands new verified chunks of the session to the `terminal` slice, which
//    applies them strictly in `scope_seq` order; when a possible gap holds chunks back, the watcher pulls from
//    `through` before anything further is rendered – one pull at a time, at most one per `PULL_GAP_MS`, so that the
//    chunks of unwatched sessions in the same workgroup do not exhaust the rate limit;
// 4. a failed pull, or a chunk that cannot be read yet, is pulled again after `RETRY_MS`.
//
// `stop()` leaves the channel's list and drops the session's chunks from memory.
import type { SignedClient } from '../net/signedFetch';
import { HubError } from '../net/signedFetch';
import { SYSTEM_TIMERS, type StoredEvent, type Timers } from '../net/sse';
import { isRecord } from '../registry';
import type { EventEntry } from '../store/slices/events';
import {
  accountedIn,
  closeTerminal,
  isChunkOf,
  openTerminal,
  pulledPage,
  pulledVerdict,
  receiveChunks,
  terminalFailed,
  type TerminalTarget,
  TERMINAL_KIND,
  verdictOf,
} from '../store/slices/terminal';
import type { Store } from '../store/store';

export const PAGE_LIMIT = 1000;
/** Minimum time between the starts of two pulls of one session. */
export const PULL_GAP_MS = 1000;
/** Delay before a failed or blocked pull is tried again. */
export const RETRY_MS = 5000;

/** What the terminal view needs of the signed-in browser (`AppSession.terminal`; tests pass doubles). */
export interface TerminalContext {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  /** Reads pulled events of a scope of `org` with the event reader, without storing them. */
  readonly read: (org: string, scopeId: string, items: readonly StoredEvent[]) => Promise<EventEntry[]>;
  /** Adds the session to the live channel's watched terminals; the returned function removes it again. */
  readonly watch: (org: string, session: string) => () => void;
  readonly timers?: Timers;
  readonly now: () => number;
}

interface Page {
  readonly events: StoredEvent[];
  readonly next_after: number | null;
  readonly head: number;
}

const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

function isPage(value: unknown): value is Page {
  return (
    isRecord(value) &&
    Array.isArray(value.events) &&
    value.events.every((item) => isRecord(item) && isSeq(item.scope_seq) && isRecord(item.event)) &&
    (value.next_after === null || isSeq(value.next_after)) &&
    isSeq(value.head)
  );
}

/** The request target of one history page after `after`. */
export function terminalTarget(target: TerminalTarget, after: number): string {
  const params = new URLSearchParams({
    after: String(after),
    limit: String(PAGE_LIMIT),
    kinds: TERMINAL_KIND,
    session: target.session,
  });
  return `/v1/scopes/${encodeURIComponent(target.wg)}/events?${params.toString()}`;
}

export class TerminalWatch {
  readonly #context: TerminalContext;
  readonly #target: TerminalTarget;
  readonly #timers: Timers;
  #release: (() => void) | null = null;
  #unsubscribe: (() => void) | null = null;
  #timer: unknown = null;
  #pulling = false;
  #again = false;
  #lastPull = Number.NEGATIVE_INFINITY;
  #stopped = false;
  /** The `events` entries already handed to the slice (by identity; a re-read entry is a new object). */
  #seen = new WeakSet<EventEntry>();

  constructor(context: TerminalContext, target: TerminalTarget) {
    this.#context = context;
    this.#target = target;
    this.#timers = context.timers ?? SYSTEM_TIMERS;
  }

  start(): void {
    const { store } = this.#context;
    openTerminal(store, this.#target);
    this.#release = this.#context.watch(this.#target.org, this.#target.session);
    this.#unsubscribe = store.subscribe(() => this.#changed());
    this.#changed();
    void this.#pull();
  }

  stop(): void {
    this.#stopped = true;
    this.#unsubscribe?.();
    this.#release?.();
    if (this.#timer !== null) this.#timers.clearTimeout(this.#timer);
    this.#timer = null;
    closeTerminal(this.#context.store, this.#target.session);
  }

  #entry() {
    return this.#context.store.getState().terminal.sessions[this.#target.session];
  }

  /** New chunks of the session in the `events` slice; a gap or a block asks for a pull. */
  #changed(): void {
    const { store } = this.#context;
    const state = store.getState();
    const entries = state.events[this.#target.wg]?.entries;
    const fresh: EventEntry[] = [];
    for (const entry of entries?.values() ?? []) {
      if (this.#seen.has(entry) || !isChunkOf(entry, this.#target.session)) continue;
      this.#seen.add(entry);
      fresh.push(entry);
    }
    const accounted = accountedIn(state.events, this.#target);
    receiveChunks(store, this.#target.session, fresh.map(verdictOf), accounted);
    // only the held chunks are checked here; the store notified this listener again if the update changed anything
    const entry = this.#entry();
    if (entry !== undefined && entry.held.length > 0) this.#schedule(0);
  }

  /** A pull after `delay` ms, not earlier than `PULL_GAP_MS` after the last one (one pending at a time). */
  #schedule(delay: number): void {
    if (this.#pulling) {
      this.#again = true;
      return;
    }
    if (this.#timer !== null) return;
    const wait = Math.max(delay, this.#lastPull + PULL_GAP_MS - this.#context.now());
    this.#timer = this.#timers.setTimeout(() => {
      this.#timer = null;
      void this.#pull();
    }, wait);
  }

  async #pull(): Promise<void> {
    this.#pulling = true;
    this.#again = false;
    this.#lastPull = this.#context.now();
    const { store, client, read } = this.#context;
    const generation = store.generation;
    try {
      for (;;) {
        const after = this.#entry()!.through;
        const page = await client.request('GET', terminalTarget(this.#target, after));
        if (!isPage(page)) throw new HubError('unavailable', 200);
        const items = page.events.filter((item) => item.scope_seq > after && item.scope_seq <= page.head);
        const entries = await read(this.#target.org, this.#target.wg, items);
        if (this.#stopped || store.generation !== generation) {
          this.#pulling = false;
          return;
        }
        const upTo = page.next_after ?? page.head;
        const accounted = accountedIn(store.getState().events, this.#target);
        const verdicts = entries.map((entry) => pulledVerdict(entry, this.#target.session));
        pulledPage(store, this.#target.session, verdicts, upTo, accounted);
        if (page.next_after === null || this.#entry()!.blockedAt !== null) break;
      }
    } catch (error) {
      this.#pulling = false;
      if (this.#stopped || store.generation !== generation) return;
      terminalFailed(store, this.#target.session, error instanceof HubError ? error.code : 'unavailable');
      this.#schedule(RETRY_MS);
      return;
    }
    this.#pulling = false;
    const entry = this.#entry()!;
    if (entry.blockedAt !== null) this.#schedule(RETRY_MS);
    else if (this.#again || entry.held.length > 0) this.#schedule(0);
  }
}
