// The reader of 04-crypto.md section 5.12 for events arriving from the live channel or from pulls: every event enters
// the store at once as `pending` and is then checked against the org's verified log (header, author signature, the
// author's right at `lh`, decertified platform devices excluded) and opened with the scope key of its epoch.
// An event with `lh` beyond the verified head first triggers the log job (L10); if the refreshed log still does not
// reach `lh`, the org is blocked (`log_fork`). The same job runs first when the org has platform devices but the
// platform log is not verified yet (a member who just accepted holds the org's log before its first log job ran):
// without it the reader cannot tell whether the author is an excluded platform device, and refusing then (`log_rights`)
// would hide a genuine event for good (KOR-05: the names of a notification stayed "…"). Failures make the entry
// `unverifiable` with the code – never dropped, never shown as content. A missing epoch key makes it `locked`;
// `retry(org)` re-reads pending and locked entries after the jobs brought a new log, a new platform log or new keys.
import { b64uDecode } from '../encoding/b64u';
import { fromUtf8 } from '../encoding/bytes';
import { CryptoError } from '../crypto/errors';
import { openEvent } from '../crypto/event';
import { refusalCode } from '../log/entries';
import { checkEvent } from '../log/events';
import { excludedFor } from '../log/platform';
import { epochKey } from '../log/state';
import { checkLh } from '../jobs/log-verify';
import type { JobRunner } from '../jobs';
import type { StoredEvent } from '../net/sse';
import { type EventEntry, findEvents, putEvents } from './slices/events';
import { orgEntry } from './slices/orgs';
import type { Store } from './store';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** `header.org`, `header.scope_id`, `header.id` and `header.lh` of a raw event, as far as they are well-formed. */
function headerOf(event: unknown): { org: unknown; scopeId: unknown; id: unknown; lh: number | null } {
  const header = isRecord(event) && isRecord(event.header) ? event.header : {};
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

export class EventReader {
  readonly #store: Store;
  readonly #runner: Pick<JobRunner, 'trigger'>;
  /** Per scope: event ID → the `scope_seq` it was first read at (E9 on the reader side: one ID, one position). */
  readonly #ids = new Map<string, Map<string, number>>();
  /** Orgs whose log job succeeded without a platform log to verify (the hub has none). */
  readonly #platformMissing = new Set<string>();

  constructor(store: Store, runner: Pick<JobRunner, 'trigger'>) {
    this.#store = store;
    this.#runner = runner;
  }

  /** Stores new events of a scope of `org` as `pending` and reads them in order. */
  async ingest(org: string, scopeId: string, items: readonly StoredEvent[]): Promise<void> {
    const entries: EventEntry[] = items.map((item) => ({
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
    putEvents(this.#store, scopeId, entries);
    for (const entry of entries) await this.#settle(entry);
  }

  /** Reads the pending and locked entries of an org again. */
  async retry(org: string): Promise<void> {
    const open = findEvents(
      this.#store.getState().events,
      (entry) => (entry.status === 'pending' || entry.status === 'locked') && entry.org === org,
    ).sort((a, b) => a.scopeSeq - b.scopeSeq);
    for (const entry of open) await this.#settle(entry);
  }

  async #settle(entry: EventEntry): Promise<void> {
    const generation = this.#store.generation;
    const next = this.#unique(await this.read(entry));
    if (next.status === 'pending' || this.#store.generation !== generation) return;
    const current = this.#store.getState().events[entry.scopeId]?.entries.get(entry.scopeSeq);
    // an entry evicted meanwhile stays evicted
    if (current === undefined) return;
    putEvents(this.#store, entry.scopeId, [next]);
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
      const checked = await checkEvent(entry.event, found.log, { excluded: excludedFor(platform, found.log.state) });
      const { header } = checked;
      const key = found.keys.get(epochKey(header.scope as string, header.scope_id as string, header.epoch as number));
      if (key === undefined) return { ...entry, status: 'locked', code: null, header, body: null };
      const plaintext = await openEvent(key, entry.event, b64uDecode(checked.authorSig));
      return { ...entry, status: 'verified', code: null, header, body: parseBody(plaintext) };
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
