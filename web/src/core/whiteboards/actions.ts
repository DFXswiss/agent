// What a board does (06-web.md section 6.1, 03-api.md sections 4.4 and 4.9, 04-crypto.md sections 4.3 and 5.12):
//
// - `loadBoard`: once per store generation the whiteboard events of the board's scope (`GET /v1/scopes/{scope}/events`,
//   task boards filtered by `task`), then every page of the entry rows (`GET /v1/whiteboards?org|wg|task&level`). All
//   events of a board are held anyway (the texts and histories come from them), so the rows are not paged in the UI. The texts come only from those events, read by the event reader; live events arrive
//   over the channel (03-api.md section 5, `event`), so a second browser sees new entries at once;
// - `loadHistory`: `GET /v1/whiteboards/{entry}/history` into the reader (every version of an entry);
// - `createEntry` / `editEntry` / `deleteEntry`: the browser encrypts the body with the scope's current key, signs it
//   (`lh` = the verified head) and sends it with `POST /v1/events` – a new entry optionally with `notify:true` (only
//   `whiteboard.entry` may carry it); the stored event enters the reader at the returned `scope_seq`. Without the
//   board's write right the write is refused before anything is sealed (`forbidden`; the UI shows no control then).
//   After `unavailable` or `rate_limited` the sealed event is kept and an identical request sends it again (same IDs,
//   06-web.md section 12.5).
import { bodyJson } from '../encoding/json';
import { uuid7 } from '../encoding/ids';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import type { Secret } from '../crypto/secret';
import { epochKey } from '../log/state';
import { HubError } from '../net/signedFetch';
import type { StoredEvent } from '../net/sse';
import { isRecord } from '../registry';
import { orgEntry } from '../store/slices/orgs';
import {
  isWhiteboardRow,
  keepWrite,
  putBoardRows,
  setBoardError,
  setBoardLoading,
  type WhiteboardRow,
  setHistoryLoad,
} from '../store/slices/whiteboards';
import { pullOnce, type TasksContext } from '../tasks/pull';
import { SendError, sendErrorCode } from '../tasks/send';
import { type BoardLocation, boardKey, boardScope, canWriteBoard } from './location';
import { DELETE, EDIT, ENTRY, validText, WHITEBOARD_KINDS } from './project';

/** Rows per page of `GET /v1/whiteboards` (the maximum; every page is loaded). */
export const PAGE_SIZE = 200;

/** Failures after which the sealed event is kept for an identical retry. */
export const RETRYABLE: ReadonlySet<string> = new Set(['unavailable', 'rate_limited']);

/** The request target of a page of entry rows. */
export function listTarget(location: BoardLocation, before: string | null): string {
  const params = new URLSearchParams({ org: location.org, level: location.level });
  if (location.wg !== null) params.set('wg', location.wg);
  if (location.task !== null) params.set('task', location.task);
  params.set('limit', String(PAGE_SIZE));
  if (before !== null) params.set('before', before);
  return `/v1/whiteboards?${params.toString()}`;
}

/** The highest `scope_seq` of a scope in the store (0 without events). */
function highWater(context: TasksContext, scopeId: string): number {
  const scope = context.store.getState().events[scopeId];
  return Math.max(0, ...(scope === undefined ? [] : scope.entries.keys()));
}

/**
 * Loads a board: first its events (once per store generation), then every page of its rows, so that every entry the
 * store held before the rows were listed is listed if the hub projected it (`checked`, `project.ts`).
 */
export async function loadBoard(context: TasksContext, location: BoardLocation): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const key = boardKey(location);
  const { scopeId } = boardScope(location);
  setBoardLoading(store, key);
  try {
    await pullOnce(context, location.org, scopeId, {
      kinds: WHITEBOARD_KINDS,
      ...(location.task !== null && { task: location.task }),
    });
    const checked = highWater(context, scopeId);
    const rows: WhiteboardRow[] = [];
    let before: string | null = null;
    do {
      const page = await context.client.request('GET', listTarget(location, before));
      if (!isRecord(page) || !Array.isArray(page.entries) || !page.entries.every(isWhiteboardRow)) {
        throw new HubError('unavailable', 200);
      }
      rows.push(...page.entries.filter((row) => !rows.some((known) => known.entry_id === row.entry_id)));
      before = typeof page.next_before === 'string' ? page.next_before : null;
    } while (before !== null);
    if (store.generation === generation) putBoardRows(store, key, rows, checked);
  } catch (error) {
    if (store.generation === generation) setBoardError(store, key, sendErrorCode(error));
  }
}

const isStored = (value: unknown): value is StoredEvent =>
  isRecord(value) &&
  Number.isSafeInteger(value.scope_seq) &&
  (value.received_at === null || typeof value.received_at === 'string') &&
  'event' in value;

/** Loads every version of an entry of the board at `location` into the reader. */
export async function loadHistory(context: TasksContext, location: BoardLocation, entry: string): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  setHistoryLoad(store, entry, 'loading');
  try {
    const body = await context.client.request('GET', `/v1/whiteboards/${encodeURIComponent(entry)}/history`);
    if (!isRecord(body) || !Array.isArray(body.events) || !body.events.every(isStored)) {
      throw new HubError('unavailable', 200);
    }
    if (store.generation !== generation) return;
    // events the reader already settled stay as they are (re-reading would make them pending for a moment)
    const { scopeId } = boardScope(location);
    const known = store.getState().events[scopeId]?.entries;
    const fresh = body.events.filter((item) => {
      const status = known?.get(item.scope_seq)?.status;
      return status !== 'verified' && status !== 'unverifiable';
    });
    await context.ingest(location.org, scopeId, fresh);
    setHistoryLoad(store, entry, 'ready');
  } catch (error) {
    if (store.generation === generation) setHistoryLoad(store, entry, 'error', sendErrorCode(error));
  }
}

/** The current key of the board's scope; asks the log and wrap jobs once if it is not in memory yet. */
async function scopeKey(context: TasksContext, location: BoardLocation): Promise<[number, Secret]> {
  const { scope, scopeId } = boardScope(location);
  const lookup = (): [number, Secret] | null => {
    const entry = orgEntry(context.store.getState().orgs, location.org);
    const epoch = entry.log!.state.epoch(scope, scopeId);
    const key = entry.keys.get(epochKey(scope, scopeId, epoch));
    return key === undefined ? null : [epoch, key];
  };
  const known = lookup();
  if (known !== null) return known;
  await context.refreshLog(location.org);
  const found = lookup();
  if (found === null) throw new SendError('epoch_stale');
  return found;
}

/** The signed-in writer of the board (`forbidden` without the board's write right). */
function writer(context: TasksContext, location: BoardLocation) {
  const { userId, identity } = context.store.getState().me;
  const entry = orgEntry(context.store.getState().orgs, location.org);
  if (userId === null || identity === null) throw new SendError('unauthorized');
  if (entry.blocked !== null) throw new SendError(entry.blocked.code);
  if (entry.log === null) throw new SendError('log_empty');
  if (!canWriteBoard(entry.log.state, userId, location)) throw new SendError('forbidden');
  return { userId, identity, log: entry.log };
}

async function seal(
  context: TasksContext,
  location: BoardLocation,
  kind: string,
  ref: string,
  body: unknown,
): Promise<unknown> {
  const { userId, identity, log } = writer(context, location);
  const [epoch, key] = await scopeKey(context, location);
  const { scope, scopeId } = boardScope(location);
  const now = context.now();
  const header = newHeader({
    id: uuid7(now),
    org: location.org,
    scope,
    scope_id: scopeId,
    epoch,
    author: userId,
    kind,
    ts: formatTime(now),
    lh: log.head.seq,
    wg: location.level === 'org' ? '' : location.wg!,
    task: location.task ?? '',
    ref,
  });
  return sealEvent(key, header, bodyJson(body), identity.sig.privateKey);
}

interface Write {
  readonly kind: string;
  /** The entry; a new entry gets a fresh ID when nothing is kept. */
  readonly ref: string | null;
  readonly body: (ref: string) => unknown;
  readonly notify: boolean;
}

/** Seals (or re-uses the kept event of an identical request), sends and reads the event; returns the entry ID. */
async function submit(context: TasksContext, location: BoardLocation, write: Write): Promise<string> {
  const { store } = context;
  const slot = `${boardKey(location)} ${write.kind} ${write.ref ?? ''}`;
  const request = JSON.stringify([write.body(''), write.notify]);
  const kept = store.getState().whiteboards.kept[slot];
  let event: unknown;
  if (kept !== undefined && kept.request === request) event = kept.event;
  else {
    const ref = write.ref ?? uuid7(context.now());
    event = await seal(context, location, write.kind, ref, write.body(ref));
  }
  const generation = store.generation;
  keepWrite(store, slot, { request, event });
  try {
    const item = write.notify ? { event, notify: true } : { event };
    const answer = await context.client.request('POST', '/v1/events', { body: { events: [item] } });
    const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : undefined;
    if (!isRecord(result) || !Number.isSafeInteger(result.scope_seq)) throw new HubError('unavailable', 200);
    // after a sign-out nothing of this write enters the new session's store
    if (store.generation !== generation) throw new SendError('unauthorized');
    keepWrite(store, slot, null);
    const { scopeId } = boardScope(location);
    await context.ingest(location.org, scopeId, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
    return (event as { header: { ref: string } }).header.ref;
  } catch (error) {
    if (store.generation === generation && !RETRYABLE.has(sendErrorCode(error))) keepWrite(store, slot, null);
    throw error;
  }
}

function checkText(text: string): void {
  if (!validText(text)) throw new SendError(text === '' ? 'request_invalid' : 'size_exceeded');
}

/** A new entry with the Markdown `text`; `notify` asks the hub to notify the board's readers (ADR-020). */
export async function createEntry(
  context: TasksContext,
  location: BoardLocation,
  text: string,
  notify: boolean,
): Promise<string> {
  checkText(text);
  return await submit(context, location, {
    kind: ENTRY,
    ref: null,
    body: () => ({ level: location.level, text }),
    notify,
  });
}

/** What an edit or delete needs of an entry: its ID and its newest applied event. */
export interface EntryRef {
  readonly id: string;
  readonly latestEventId: string;
}

export async function editEntry(
  context: TasksContext,
  location: BoardLocation,
  entry: EntryRef,
  text: string,
): Promise<void> {
  checkText(text);
  await submit(context, location, {
    kind: EDIT,
    ref: entry.id,
    body: () => ({ text, base_event_id: entry.latestEventId }),
    notify: false,
  });
}

export async function deleteEntry(context: TasksContext, location: BoardLocation, entry: EntryRef): Promise<void> {
  await submit(context, location, {
    kind: DELETE,
    ref: entry.id,
    body: () => ({ base_event_id: entry.latestEventId }),
    notify: false,
  });
}
