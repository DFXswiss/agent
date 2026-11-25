// The action record of one session's platform browser (06-web.md section 5.4b, ADR-085 section 4): the session's
// `browser.action` events, read with `GET /v1/scopes/{wg}/events?session=…&kinds=browser.action`.
//
// The events route pages only forward (`after`, ascending `scope_seq`; there is no `before`), so the record is read
// in pages of `RECORD_PAGE` events from the start up to the hub's head, and the view shows it newest first. What is
// kept is bounded by the pictures, not by the entries:
//
// - every entry is kept **without** its picture (`RecordEntry`: the body with `image` reduced to type and size);
// - the picture data (b64u) of at most `PICTURES` entries is kept, the most recently read or used ones (after the
//   first pass the newest ones, which the view shows first); the picture of any other entry is read again on demand
//   with `after=<scope_seq − 1>&limit=1` (`picture`), only for rows in view;
// - entries arrive live as `browser_action` frames (`receive`; the stream does not replay them on a reconnect, so
//   `pull` continues from the last read position after every (re)connect and fetches what was missed);
// - entries are de-duplicated by `scope_seq`; an entry that cannot be verified, or whose body does not follow the
//   contract, stays as a neutral row with its code (never dropped, never shown as content); a verified event of
//   another session or kind is not part of the record.
//
// A failed page is read again after `RETRY_MS`; `stop()` ends every timer and ignores answers still under way.
import { b64uDecode } from '../encoding/b64u';
import { parseTime } from '../encoding/time';
import { HubError } from '../net/signedFetch';
import { SYSTEM_TIMERS, type StoredEvent, type Timers } from '../net/sse';
import { isRecord } from '../registry';
import type { EventEntry } from '../store/slices/events';
import type { TerminalContext } from '../terminal/watch';
import { ACTION_KIND, type ActionImage, type BrowserAction, parseBrowserAction } from './shape';

/** Events per page (one event is up to about 140 KiB, mostly its picture). */
export const RECORD_PAGE = 20;
/** Pictures (b64u) kept in memory. */
export const PICTURES = 32;
/** Delay before a failed page is read again. */
export const RETRY_MS = 5000;

export interface RecordTarget {
  readonly org: string;
  readonly wg: string;
  readonly session: string;
}

/** What the record needs of the signed-in browser (`AppSession.browser`; tests pass doubles). */
export interface RecordContext {
  readonly client: TerminalContext['client'];
  /** Reads events of a scope with the event reader, without storing them. */
  readonly read: TerminalContext['read'];
  readonly timers?: Timers;
}

/** An action without its picture data. */
export type RecordedAction = Omit<BrowserAction, 'image'> & { readonly image: Omit<ActionImage, 'data'> | null };

export interface RecordEntry {
  readonly scopeSeq: number;
  /** The action, or null when the event could not be read (`code`). */
  readonly action: RecordedAction | null;
  /** Unix ms of `action.at`, 0 without an action. */
  readonly at: number;
  readonly code: string | null;
}

export interface RecordState {
  /** Ascending `scope_seq`; the view shows them newest first. */
  readonly entries: readonly RecordEntry[];
  /** A page is being read. */
  readonly loading: boolean;
  /** Everything up to the hub's head at the last page was read. */
  readonly complete: boolean;
  /** Code of the last failed page, null after a successful one. */
  readonly error: string | null;
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

/** The request target of the page after `after` (`limit` events). */
export function recordTarget(target: RecordTarget, after: number, limit: number = RECORD_PAGE): string {
  const params = new URLSearchParams({
    after: String(after),
    limit: String(limit),
    kinds: ACTION_KIND,
    session: target.session,
  });
  return `/v1/scopes/${encodeURIComponent(target.wg)}/events?${params.toString()}`;
}

/** The record entry and picture of a read event, or null when it is not one of the session's actions. */
function entryOf(
  entry: EventEntry,
  session: string,
): { readonly entry: RecordEntry; readonly data: string | null } | null {
  if (entry.status !== 'verified') {
    return { entry: { scopeSeq: entry.scopeSeq, action: null, at: 0, code: entry.code ?? entry.status }, data: null };
  }
  if (entry.header!.kind !== ACTION_KIND || entry.header!.session !== session) return null;
  const body = parseBrowserAction(entry.body);
  if (body === null) {
    return { entry: { scopeSeq: entry.scopeSeq, action: null, at: 0, code: 'body_invalid' }, data: null };
  }
  const { image, ...rest } = body;
  const action: RecordedAction = {
    ...rest,
    image: image === null ? null : { type: image.type, width: image.width, height: image.height },
  };
  return { entry: { scopeSeq: entry.scopeSeq, action, at: parseTime(body.at), code: null }, data: image?.data ?? null };
}

/** The index of the first entry with a `scope_seq` of at least `seq`. */
function lowerBound(entries: readonly RecordEntry[], seq: number): number {
  let low = 0;
  let high = entries.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (entries[middle]!.scopeSeq < seq) low = middle + 1;
    else high = middle;
  }
  return low;
}

export class ActionRecord {
  readonly #context: RecordContext;
  readonly #target: RecordTarget;
  readonly #timers: Timers;
  readonly #listeners = new Set<() => void>();
  #state: RecordState = { entries: [], loading: false, complete: false, error: null };
  /** Every matching event up to here was read. */
  #through = 0;
  #pulling = false;
  #again = false;
  #timer: unknown = null;
  #stopped = false;
  /** Picture data by `scope_seq`, least recently used first. */
  readonly #pictures = new Map<number, string>();
  readonly #fetching = new Map<number, Promise<Uint8Array | null>>();

  constructor(context: RecordContext, target: RecordTarget) {
    this.#context = context;
    this.#target = target;
    this.#timers = context.timers ?? SYSTEM_TIMERS;
  }

  get state(): RecordState {
    return this.#state;
  }

  /** The number of pictures held in memory (bounded by `PICTURES`). */
  get pictures(): number {
    return this.#pictures.size;
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #set(patch: Partial<RecordState>): void {
    this.#state = { ...this.#state, ...patch };
    for (const listener of [...this.#listeners]) listener();
  }

  stop(): void {
    this.#stopped = true;
    if (this.#timer !== null) this.#timers.clearTimeout(this.#timer);
    this.#timer = null;
    this.#listeners.clear();
    this.#pictures.clear();
  }

  #keep(seq: number, data: string): void {
    this.#pictures.delete(seq);
    this.#pictures.set(seq, data);
    while (this.#pictures.size > PICTURES) this.#pictures.delete(this.#pictures.keys().next().value!);
  }

  /** Adds read events (de-duplicated, in `scope_seq` order) and keeps their pictures. */
  #add(read: readonly EventEntry[]): void {
    const entries = [...this.#state.entries];
    let changed = false;
    for (const item of read) {
      const found = entryOf(item, this.#target.session);
      if (found === null) continue;
      const at = lowerBound(entries, found.entry.scopeSeq);
      if (entries[at]?.scopeSeq === found.entry.scopeSeq) continue;
      entries.splice(at, 0, found.entry);
      changed = true;
      if (found.data !== null) this.#keep(found.entry.scopeSeq, found.data);
    }
    if (changed) this.#set({ entries });
  }

  /** Reads the record from the last read position up to the hub's head (one pull at a time). */
  async pull(): Promise<void> {
    if (this.#stopped) return;
    if (this.#pulling) {
      this.#again = true;
      return;
    }
    if (this.#timer !== null) {
      this.#timers.clearTimeout(this.#timer);
      this.#timer = null;
    }
    this.#pulling = true;
    this.#again = false;
    this.#set({ loading: true });
    try {
      for (;;) {
        const after = this.#through;
        const page = await this.#context.client.request('GET', recordTarget(this.#target, after));
        if (!isPage(page)) throw new HubError('unavailable', 200);
        const items = page.events.filter((item) => item.scope_seq > after && item.scope_seq <= page.head);
        const read = await this.#context.read(this.#target.org, this.#target.wg, items);
        if (this.#stopped) return;
        this.#add(read);
        this.#through = Math.max(after, page.next_after ?? page.head);
        if (page.next_after === null) break;
      }
    } catch (error) {
      this.#pulling = false;
      if (this.#stopped) return;
      this.#set({ loading: false, error: error instanceof HubError ? error.code : 'unavailable' });
      this.#timer = this.#timers.setTimeout(() => {
        this.#timer = null;
        void this.pull();
      }, RETRY_MS);
      return;
    }
    this.#pulling = false;
    this.#set({ loading: false, complete: true, error: null });
    if (this.#again) void this.pull();
  }

  /** A `browser_action` frame (`{scope_id, scope_seq, event}`) of the watched session. */
  async receive(data: unknown): Promise<void> {
    if (!isRecord(data) || data.scope_id !== this.#target.wg || !isSeq(data.scope_seq) || !isRecord(data.event)) return;
    const item: StoredEvent = { scope_seq: data.scope_seq, received_at: null, event: data.event };
    const read = await this.#context.read(this.#target.org, this.#target.wg, [item]);
    if (!this.#stopped) this.#add(read);
  }

  /** The JPEG of the entry at `scopeSeq` (kept, or read again), null when it has none or cannot be read. */
  picture(scopeSeq: number): Promise<Uint8Array | null> {
    const kept = this.#pictures.get(scopeSeq);
    if (kept !== undefined) {
      this.#keep(scopeSeq, kept);
      return Promise.resolve(b64uDecode(kept));
    }
    const entries = this.#state.entries;
    const entry = entries[lowerBound(entries, scopeSeq)];
    if (entry?.scopeSeq !== scopeSeq || entry.action?.image == null) return Promise.resolve(null);
    const running = this.#fetching.get(scopeSeq);
    if (running !== undefined) return running;
    const fetched = this.#fetchPicture(scopeSeq).finally(() => this.#fetching.delete(scopeSeq));
    this.#fetching.set(scopeSeq, fetched);
    return fetched;
  }

  async #fetchPicture(scopeSeq: number): Promise<Uint8Array | null> {
    try {
      const page = await this.#context.client.request('GET', recordTarget(this.#target, scopeSeq - 1, 1));
      if (!isPage(page)) return null;
      const items = page.events.filter((item) => item.scope_seq === scopeSeq);
      const read = await this.#context.read(this.#target.org, this.#target.wg, items);
      const found = read.length === 0 ? null : entryOf(read[0]!, this.#target.session);
      if (found?.data == null || this.#stopped) return null;
      this.#keep(scopeSeq, found.data);
      return b64uDecode(found.data);
    } catch {
      return null;
    }
  }
}
