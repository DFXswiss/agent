// Watching one session's terminal (06-web.md section 5.5, 03-api.md sections 4.4 and 5, ADR-086 sections 1 and 2):
//
// 1. the session joins the live channel's `terminal=` list (`TerminalContext.watch`, at most 3 per stream), so its
//    chunks arrive as `terminal` frames, pass the event reader and land in the `events` slice; the view registers the
//    session as shown (`core/store/focus.ts`), so the reader does not skip its live bulk events;
// 2. on open, the **newest** page of `GET /v1/scopes/{wg}/events?session=…&kinds=session.terminal` is read
//    (`eventsBefore`, newest first, with the old-hub fallback) with the reader *without* entering the `events` slice
//    (a long history would push the chat out of its bounded store); older output stays on the hub (`earlier`);
// 3. every change of the `events` slice hands new verified chunks of the session to the `terminal` slice, which
//    applies them strictly in `scope_seq` order; when a possible gap holds chunks back, the watcher pulls ascending
//    from `through` before anything further is rendered – one pull at a time, at most one per `PULL_GAP_MS`, so that
//    the chunks of unwatched sessions in the same workgroup do not exhaust the rate limit. A repair reads at most
//    `REPAIR_PAGES` pages; a longer gap reads the newest page again instead (the tail starts anew, the output before
//    it stays on the hub). After the live channel's `resync` (`refresh`) the same bounded repair runs;
// 4. a failed pull, or a chunk that cannot be read yet, is pulled again after `RETRY_MS`;
// 5. History mode: `history()` opens the window (a copy of the tail), `loadEarlier()` puts the page before it in front
//    (newest first from the hub, on demand), `follow()` closes it.
//
// `stop()` leaves the channel's list, ends the registration and drops the session's chunks from memory.
import { eventsBefore, eventsTarget, NEWEST, PAGE_DEFAULT, PAGE_MAX } from '../net/pages';
import type { SignedClient } from '../net/signedFetch';
import { HubError } from '../net/signedFetch';
import { SYSTEM_TIMERS, type StoredEvent, type Timers } from '../net/sse';
import { isRecord } from '../registry';
import { show } from '../store/focus';
import type { EventEntry } from '../store/slices/events';
import { terminalFailed, type TerminalTarget, TERMINAL_KIND } from '../store/slices/terminal';
import {
  accountedIn,
  closeTerminal,
  earlierPage,
  earlierPageFailed,
  isChunkOf,
  loadingEarlier,
  newestPage,
  openTerminal,
  pulledPage,
  pulledVerdict,
  receiveChunks,
  setHistory,
  verdictOf,
} from '../terminal/entries';
import type { Store } from '../store/store';

/** Chunks of the newest page and of an earlier page. */
export const PAGE_LIMIT = PAGE_DEFAULT;
/** Chunks of one ascending repair page, and the pages a repair reads at most before it reads the newest page. */
export const REPAIR_LIMIT = PAGE_MAX;
export const REPAIR_PAGES = 3;
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

const queryOf = (target: TerminalTarget) => ({ kinds: [TERMINAL_KIND], session: target.session });

/** The request target of one ascending repair page after `after`. */
export function terminalTarget(target: TerminalTarget, after: number): string {
  return eventsTarget(target.wg, queryOf(target), { after }, REPAIR_LIMIT);
}

const codeOf = (error: unknown): string => (error instanceof HubError ? error.code : 'unavailable');

export class TerminalWatch {
  readonly #context: TerminalContext;
  readonly #target: TerminalTarget;
  readonly #timers: Timers;
  #release: (() => void) | null = null;
  #unfocus: (() => void) | null = null;
  #unsubscribe: (() => void) | null = null;
  #timer: unknown = null;
  #pulling = false;
  #again = false;
  /** The next pull reads the newest page (on open, and after a gap too long to repair). */
  #newest = true;
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
    this.#unfocus = show(store, {
      scope: this.#target.wg,
      session: this.#target.session,
      refresh: () => this.refresh(),
    });
    this.#unsubscribe = store.subscribe(() => this.#changed());
    this.#changed();
    void this.#pull();
  }

  stop(): void {
    this.#stopped = true;
    this.#unsubscribe?.();
    this.#release?.();
    this.#unfocus?.();
    if (this.#timer !== null) this.#timers.clearTimeout(this.#timer);
    this.#timer = null;
    closeTerminal(this.#context.store, this.#target.session);
  }

  /** The live channel lost frames (`resync`): pull from `through` (bounded; a long gap reads the newest page). */
  refresh(): void {
    if (!this.#stopped) this.#schedule(0);
  }

  /** History mode: the window over the tail (`setHistory`). */
  history(): void {
    setHistory(this.#context.store, this.#target.session, true);
  }

  /** Follow mode: the window leaves memory. */
  follow(): void {
    setHistory(this.#context.store, this.#target.session, false);
  }

  /** Puts the page before the history window in front of it (nothing while one loads or at the start). */
  async loadEarlier(): Promise<void> {
    const { store, client, read } = this.#context;
    const window = this.#entry()?.history ?? null;
    if (window === null || window.before === null || window.status === 'loading') return;
    const before = window.before;
    const generation = store.generation;
    const { session } = this.#target;
    loadingEarlier(store, session, before);
    try {
      const page = await eventsBefore(client, this.#target.wg, queryOf(this.#target), before, PAGE_LIMIT);
      const entries = await read(this.#target.org, this.#target.wg, page.events);
      if (this.#gone(generation)) return;
      const verdicts = entries.map((entry) => pulledVerdict(entry, session));
      earlierPage(store, session, before, verdicts, page.nextBefore);
    } catch (error) {
      if (!this.#gone(generation)) earlierPageFailed(store, session, before, codeOf(error));
    }
  }

  #entry() {
    return this.#context.store.getState().terminal.sessions[this.#target.session];
  }

  #gone(generation: number): boolean {
    return this.#stopped || this.#context.store.generation !== generation;
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
    // after a sign-out (`store.reset`) the session has no entry until the view opens it again
    if (this.#entry() === undefined) return;
    this.#pulling = true;
    this.#again = false;
    this.#lastPull = this.#context.now();
    const { store } = this.#context;
    const generation = store.generation;
    let failed: string | null = null;
    try {
      await (this.#newest ? this.#readNewest(generation) : this.#repair(generation));
    } catch (error) {
      failed = codeOf(error);
    }
    this.#pulling = false;
    if (this.#gone(generation)) return;
    if (failed !== null) {
      terminalFailed(store, this.#target.session, failed);
      this.#schedule(RETRY_MS);
      return;
    }
    const entry = this.#entry()!;
    if (entry.blockedAt !== null) this.#schedule(RETRY_MS);
    else if (this.#again || entry.held.length > 0) this.#schedule(0);
  }

  /** The newest page: the tail starts anew with it. */
  async #readNewest(generation: number): Promise<void> {
    const { store, client, read } = this.#context;
    const { session } = this.#target;
    const page = await eventsBefore(client, this.#target.wg, queryOf(this.#target), NEWEST, PAGE_LIMIT);
    const entries = await read(this.#target.org, this.#target.wg, page.events);
    if (this.#gone(generation)) return;
    const upTo = Math.max(page.head, page.events.at(-1)?.scope_seq ?? 0);
    const verdicts = entries.map((entry) => pulledVerdict(entry, session));
    newestPage(store, session, verdicts, upTo, page.nextBefore, accountedIn(store.getState().events, this.#target));
    this.#newest = false;
  }

  /** Ascending pages from `through`, at most `REPAIR_PAGES`; a longer gap reads the newest page instead. */
  async #repair(generation: number): Promise<void> {
    const { store, client, read } = this.#context;
    const { session } = this.#target;
    for (let pages = 0; ; pages += 1) {
      if (pages === REPAIR_PAGES) {
        this.#newest = true;
        await this.#readNewest(generation);
        return;
      }
      const after = this.#entry()!.through;
      const page = await client.request('GET', terminalTarget(this.#target, after));
      if (!isPage(page)) throw new HubError('unavailable', 200);
      const items = page.events.filter((item) => item.scope_seq > after && item.scope_seq <= page.head);
      const entries = await read(this.#target.org, this.#target.wg, items);
      if (this.#gone(generation)) return;
      const upTo = page.next_after ?? page.head;
      const accounted = accountedIn(store.getState().events, this.#target);
      const verdicts = entries.map((entry) => pulledVerdict(entry, session));
      pulledPage(store, session, verdicts, upTo, accounted);
      if (page.next_after === null || this.#entry()!.blockedAt !== null) return;
    }
  }
}
