// The reader of 04-crypto.md section 5.12 for events arriving from the live channel or from pulls: every event enters
// the store at once as `pending` and is then checked against the org's verified log (header, author signature, the
// author's right at `lh`, decertified platform devices excluded, persons of DFX support from the support authors of the
// verified platform log, ADR-096) and opened with the scope key of its epoch.
// An event with `lh` beyond the verified head first triggers the log job (L10); if the refreshed log still does not
// reach `lh`, the org is blocked (`log_fork`). The same job runs first when the org has platform devices but the
// platform log is not verified yet (a member who just accepted holds the org's log before its first log job ran):
// without it the reader cannot tell whether the author is an excluded platform device, and refusing then (`log_rights`)
// would hide a genuine event for good (KOR-05: the names of a notification stayed "…"). Failures make the entry
// `unverifiable` with the code – never dropped, never shown as content. A missing epoch key makes it `locked`;
// `retry(org)` re-reads pending and locked entries after the jobs brought a new log, a new platform log or new keys.
// Writes are batched (HOT-05): settled entries are written to the store at most once per frame (`FRAME_MS`), so a burst
// of thousands of events costs a few store updates (and timeline builds) instead of one per event. `ingest` and
// `retry` write their last batch before they return; the live channel passes `defer` and leaves it to the frame timer.
// The live channel also passes `skipUnshown` (ADR-086 section 1): an event of a bulk kind whose (unchecked) header
// names a task and session no mounted view shows (`focus.ts`) is neither decrypted nor stored – the channel's cursor
// moves on – and the registry notes it, so that the view that opens the task later reads its newest page.
import { b64uDecode } from '../encoding/b64u';
import { fromUtf8 } from '../encoding/bytes';
import { CryptoError } from '../crypto/errors';
import { openEvent } from '../crypto/event';
import { refusalCode } from '../log/entries';
import { checkEvent } from '../log/events';
import { excludedFor } from '../log/platform';
import { epochKey } from '../log/state';
import { checkLh } from '../jobs/log-verify';
import { authorsOf } from '../support/names';
import type { JobRunner } from '../jobs';
import type { StoredEvent, Timers } from '../net/sse';
import { noteSkipped, skips } from './focus';
import { type EventEntry, findEvents, putEvents } from './slices/events';
import { orgEntry } from './slices/orgs';
import type { Store } from './store';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** The raw (unchecked) header of an event, `{}` if it has none. */
const rawHeader = (event: unknown): Record<string, unknown> =>
  isRecord(event) && isRecord(event.header) ? event.header : {};

/** `header.org`, `header.scope_id`, `header.id` and `header.lh` of a raw event, as far as they are well-formed. */
function headerOf(event: unknown): { org: unknown; scopeId: unknown; id: unknown; lh: number | null } {
  const header = rawHeader(event);
  return {
    org: header.org,
    scopeId: header.scope_id,
    id: header.id,
    lh: Number.isSafeInteger(header.lh) ? (header.lh as number) : null,
  };
}

const unverifiable = (entry: EventEntry, code: string): EventEntry => ({
  ...entry,
  status: 'unverifiable',
  code,
  header: null,
  body: null,
});

/** Minimum time between two batched writes (one frame at 60 Hz). */
export const FRAME_MS = 16;

export interface ReaderClock extends Timers {
  now(): number;
}

const SYSTEM_CLOCK: ReaderClock = {
  now: () => Date.now(),
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>),
};

/** A buffered write: `fresh` entries are new (`pending`), the others replace an entry only while it is stored. */
interface Buffered {
  readonly entry: EventEntry;
  readonly fresh: boolean;
}

export interface IngestOptions {
  /** Leave the last batch to the frame timer (the live channel: many small deliveries in a row). */
  readonly defer?: boolean;
  /** Skip bulk events no mounted view shows (the live channel, module comment). */
  readonly skipUnshown?: boolean;
}

export class EventReader {
  readonly #store: Store;
  readonly #runner: Pick<JobRunner, 'trigger'>;
  /** Per scope: event ID → the `scope_seq` it was first read at (E9 on the reader side: one ID, one position). */
  readonly #ids = new Map<string, Map<string, number>>();
  /** Orgs whose log job succeeded without a platform log to verify (the hub has none). */
  readonly #platformMissing = new Set<string>();
  readonly #clock: ReaderClock;
  /** Per scope: buffered writes by `scope_seq`, written by `flush`. */
  #buffer = new Map<string, Map<number, Buffered>>();
  /** The store generation the buffer was started in (a sign-out drops it). */
  #bufferGeneration = 0;
  #timer: unknown = null;
  #lastFlush = Number.NEGATIVE_INFINITY;

  constructor(store: Store, runner: Pick<JobRunner, 'trigger'>, clock: ReaderClock = SYSTEM_CLOCK) {
    this.#store = store;
    this.#runner = runner;
    this.#clock = clock;
  }

  /** Stores new events of a scope of `org` as `pending` and reads them in order. */
  async ingest(
    org: string,
    scopeId: string,
    items: readonly StoredEvent[],
    options: IngestOptions = {},
  ): Promise<void> {
    const shown =
      options.skipUnshown === true
        ? items.filter((item) => {
            const header = rawHeader(item.event);
            if (!skips(this.#store, header.kind, header.task, header.session)) return true;
            noteSkipped(this.#store, header.task, header.session);
            return false;
          })
        : items;
    const entries: EventEntry[] = shown.map((item) => ({
      org,
      scopeId,
      scopeSeq: item.scope_seq,
      receivedAt: item.received_at,
      event: item.event,
      status: 'pending',
      code: null,
      header: null,
      body: null,
    }));
    if (options.defer === true) this.#queue(entries, true);
    else {
      this.flush();
      putEvents(this.#store, scopeId, entries);
    }
    for (const entry of entries) await this.#settle(entry);
    if (options.defer !== true) this.flush();
  }

  /** Reads the pending and locked entries of an org again. */
  async retry(org: string): Promise<void> {
    // entries of the live channel may still wait in the buffer: retry must see them
    this.flush();
    const open = findEvents(
      this.#store.getState().events,
      (entry) => (entry.status === 'pending' || entry.status === 'locked') && entry.org === org,
    ).sort((a, b) => a.scopeSeq - b.scopeSeq);
    for (const entry of open) await this.#settle(entry);
    this.flush();
  }

  /** Writes the buffered entries now (one store update per scope). */
  flush(): void {
    if (this.#timer !== null) this.#clock.clearTimeout(this.#timer);
    this.#timer = null;
    if (this.#buffer.size === 0) return;
    const buffer = this.#buffer;
    this.#buffer = new Map();
    this.#lastFlush = this.#clock.now();
    if (this.#store.generation !== this.#bufferGeneration) return;
    for (const [scopeId, writes] of buffer) {
      const stored = this.#store.getState().events[scopeId]?.entries;
      // an entry evicted meanwhile stays evicted
      const entries = [...writes.values()]
        .filter((write) => write.fresh || stored?.has(write.entry.scopeSeq) === true)
        .map((write) => write.entry);
      putEvents(this.#store, scopeId, entries);
    }
  }

  #queue(entries: readonly EventEntry[], fresh: boolean): void {
    // a buffer of an earlier store generation (before a sign-out) is dropped
    if (this.#buffer.size > 0 && this.#bufferGeneration !== this.#store.generation) this.#buffer = new Map();
    if (this.#buffer.size === 0) this.#bufferGeneration = this.#store.generation;
    for (const entry of entries) {
      const writes = this.#buffer.get(entry.scopeId) ?? new Map<number, Buffered>();
      this.#buffer.set(entry.scopeId, writes);
      writes.set(entry.scopeSeq, { entry, fresh: fresh || writes.get(entry.scopeSeq)?.fresh === true });
    }
    if (this.#timer !== null) return;
    const wait = Math.max(0, this.#lastFlush + FRAME_MS - this.#clock.now());
    this.#timer = this.#clock.setTimeout(() => this.flush(), wait);
  }

  async #settle(entry: EventEntry): Promise<void> {
    const generation = this.#store.generation;
    const next = this.#unique(await this.read(entry));
    if (next.status === 'pending' || this.#store.generation !== generation) return;
    this.#queue([next], false);
  }

  /** A second position of an already read event ID is `event_conflict` (the hub repeated or moved it). */
  #unique(entry: EventEntry): EventEntry {
    if (entry.status !== 'verified' && entry.status !== 'locked') return entry;
    const id = entry.header!.id as string;
    const ids = this.#ids.get(entry.scopeId) ?? new Map<string, number>();
    this.#ids.set(entry.scopeId, ids);
    const seen = ids.get(id);
    if (seen !== undefined && seen !== entry.scopeSeq) return unverifiable(entry, 'event_conflict');
    ids.set(id, entry.scopeSeq);
    return entry;
  }

  /**
   * The reader's verdict on one entry. The header must name the org and scope the event was delivered for. It stays
   * `pending` while the org's log (or the platform log it needs) cannot be verified (a failed refresh is retried later
   * and never blocks the org).
   */
  async read(entry: EventEntry): Promise<EventEntry> {
    const { org, scopeId, lh } = headerOf(entry.event);
    if (org !== entry.org || scopeId !== entry.scopeId || lh === null) return unverifiable(entry, 'header_invalid');
    let found = orgEntry(this.#store.getState().orgs, entry.org);
    const platformUnread =
      found.log !== null &&
      this.#store.getState().orgs.platform === null &&
      !this.#platformMissing.has(entry.org) &&
      [...found.log.state.principals.values()].some((principal) => principal.platformSeq !== null);
    if (found.blocked === null && (found.log === null || lh > found.log.head.seq || platformUnread)) {
      const refreshed = await this.#runner.trigger('lh', entry.org);
      // a successful run that found no platform log: later events of the org need no run of their own
      if (refreshed && this.#store.getState().orgs.platform === null) this.#platformMissing.add(entry.org);
      found = orgEntry(this.#store.getState().orgs, entry.org);
      if (refreshed && found.log !== null && !checkLh(this.#store, entry.org, lh)) {
        found = orgEntry(this.#store.getState().orgs, entry.org);
      }
      if (!refreshed && found.blocked === null) return entry;
    }
    if (found.blocked !== null) return unverifiable(entry, found.blocked.code);
    if (found.log === null) return entry;
    try {
      const platform = this.#store.getState().orgs.platform?.state ?? null;
      // events of DFX support persons are checked against the support authors of the platform log (ADR-096)
      const checked = await checkEvent(entry.event, found.log, {
        excluded: excludedFor(platform, found.log.state),
        support: authorsOf(platform),
      });
      const { header } = checked;
      const key = found.keys.get(epochKey(header.scope as string, header.scope_id as string, header.epoch as number));
      if (key === undefined) return { ...entry, status: 'locked', code: null, header, body: null };
      const plaintext = await openEvent(key, entry.event, b64uDecode(checked.authorSig));
      const body = parseBody(plaintext);
      return { ...entry, status: 'verified', code: null, header, body, bodyBytes: plaintext.length };
    } catch (error) {
      return unverifiable(entry, refusalCode(error));
    }
  }
}

/** The UTF-8 JSON body of an event; anything else is `header_invalid` (the body schema is the kind's business). */
function parseBody(plaintext: Uint8Array): unknown {
  try {
    return JSON.parse(fromUtf8(plaintext)) as unknown;
  } catch {
    throw new CryptoError('header_invalid');
  }
}
