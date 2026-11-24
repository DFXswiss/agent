// What a board does (06-web.md section 6.1, 03-api.md sections 4.4 and 4.9, 04-crypto.md sections 4.3 and 5.12):
//
// - `loadBoard`: once per store generation the whiteboard events of the board's scope (`GET /v1/scopes/{scope}/events`,
//   task boards filtered by `task`; newest first, at most `BOARD_PAGES` pages, ADR-086), then the first page of the
//   entry rows (`GET /v1/whiteboards?org|wg|task&level&limit`, newest first); `loadBoardPage` reads the next older page
//   on demand ("Load more", ADR-086 section 2) – never every page in a loop. An entry whose row is on a page not read
//   yet is not taken for unlisted (`project.ts`, paged rows). The texts come only from the events, read by the event
//   reader; live events arrive over the channel (03-api.md section 5, `event`), so a second browser sees new entries at
//   once;
// - `loadHistory`: the newest page of an entry's history (`GET /v1/whiteboards/{entry}/history?limit`, newest first)
//   into the reader; `loadHistoryPage` the next older page on demand. A hub without paging answers `before`/`limit`
//   with `400 request_invalid`: the request is repeated once without them (the whole history, capped by the hub) and
//   the hub is remembered (per signed client) as one without paging; an answer without `next_before` has no older page;
// - `boardPaging` / `historyPaging`: the `ListPaging` of a board's rows and of a history for the windowed lists;
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
import type { ListPaging } from '../lists/window';
import { HubError } from '../net/signedFetch';
import type { StoredEvent } from '../net/sse';
import { isRecord } from '../registry';
import { orgEntry } from '../store/slices/orgs';
import {
  appendBoardRows,
  type BoardLoad,
  isWhiteboardRow,
  keepWrite,
  type Load,
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

/** Rows per page of `GET /v1/whiteboards` (the hub's maximum; further pages on demand). */
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

/** Newest pages of whiteboard events a board reads (10 000 events: the hub's bound of one entry's history). */
export const BOARD_PAGES = 10;

/** One page of rows (`before`: the cursor of the older page, null for the newest). */
async function rowsPage(
  context: TasksContext,
  location: BoardLocation,
  before: string | null,
): Promise<{ rows: WhiteboardRow[]; next: string | null }> {
  const page = await context.client.request('GET', listTarget(location, before));
  if (!isRecord(page) || !Array.isArray(page.entries) || !page.entries.every(isWhiteboardRow)) {
    throw new HubError('unavailable', 200);
  }
  return { rows: page.entries, next: typeof page.next_before === 'string' ? page.next_before : null };
}

/**
 * Loads a board: first its events (once per store generation, the newest `BOARD_PAGES` pages), then the first page of
 * its rows with the scope position the store held before (`checked`, `project.ts`); older pages via `loadBoardPage`.
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
      pages: BOARD_PAGES,
      ...(location.task !== null && { task: location.task }),
    });
    const checked = highWater(context, scopeId);
    const { rows, next } = await rowsPage(context, location, null);
    if (store.generation === generation) putBoardRows(store, key, rows, checked, next);
  } catch (error) {
    if (store.generation === generation) setBoardError(store, key, sendErrorCode(error));
  }
}

/**
 * Reads the next older page of a loaded board's rows. Does nothing while the board loads, before its first page or
 * when every row is listed; an answer for a cursor the board no longer has (a reload meanwhile) is dropped.
 */
export async function loadBoardPage(context: TasksContext, location: BoardLocation): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const key = boardKey(location);
  const board = store.getState().whiteboards.boards[key];
  if (board === undefined || board.status === 'loading' || board.next === null) return;
  const cursor = board.next;
  setBoardLoading(store, key);
  try {
    const { rows, next } = await rowsPage(context, location, cursor);
    if (store.generation === generation && store.getState().whiteboards.boards[key]!.next === cursor) {
      appendBoardRows(store, key, rows, next);
    }
  } catch (error) {
    if (store.generation === generation) setBoardError(store, key, sendErrorCode(error));
  }
}

/** The server paging of a board's rows for its windowed list. */
export function boardPaging(context: TasksContext, location: BoardLocation, load: BoardLoad | null): ListPaging {
  return {
    hasMore: load !== null && load.next !== null,
    loading: load?.status === 'loading',
    loadMore: () => void loadBoardPage(context, location),
  };
}

const isStored = (value: unknown): value is StoredEvent =>
  isRecord(value) &&
  Number.isSafeInteger(value.scope_seq) &&
  (value.received_at === null || typeof value.received_at === 'string') &&
  'event' in value;

/** Versions per page of a history (the hub's default page). */
export const HISTORY_PAGE = 50;

/** The request target of a page of a history (`before` null: the newest page); `paged` false: the whole history. */
export function historyTarget(entry: string, before: number | null, paged = true): string {
  const path = `/v1/whiteboards/${encodeURIComponent(entry)}/history`;
  if (!paged) return path;
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE) });
  if (before !== null) params.set('before', String(before));
  return `${path}?${params.toString()}`;
}

/** Hubs (signed clients) that refused the paging parameters of the history. */
const unpaged = new WeakSet<object>();

/** One page of a history (module comment: an old hub is asked once more without paging). */
async function historyPage(context: TasksContext, entry: string, before: number | null): Promise<unknown> {
  const { client } = context;
  if (!unpaged.has(client)) {
    try {
      return await client.request('GET', historyTarget(entry, before));
    } catch (error) {
      if (!(error instanceof HubError && error.code === 'request_invalid')) throw error;
      unpaged.add(client);
    }
  }
  return await client.request('GET', historyTarget(entry, null, false));
}

/** Reads one page of a history into the reader and records the next older page. */
async function readHistory(
  context: TasksContext,
  location: BoardLocation,
  entry: string,
  before: number | null,
): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  setHistoryLoad(store, entry, 'loading');
  try {
    const body = await historyPage(context, entry, before);
    if (!isRecord(body) || !Array.isArray(body.events) || !body.events.every(isStored)) {
      throw new HubError('unavailable', 200);
    }
    if (store.generation !== generation) return;
    // events the reader already settled stay as they are (re-reading would make them pending for a moment)
    const { scopeId } = boardScope(location);
    const known = store.getState().events[scopeId]?.entries;
    const fresh = body.events
      .filter((item) => {
        const status = known?.get(item.scope_seq)?.status;
        return status !== 'verified' && status !== 'unverifiable';
      })
      .sort((a, b) => a.scope_seq - b.scope_seq);
    await context.ingest(location.org, scopeId, fresh);
    const next = Number.isSafeInteger(body.next_before) ? (body.next_before as number) : null;
    setHistoryLoad(store, entry, 'ready', null, next);
  } catch (error) {
    if (store.generation === generation) setHistoryLoad(store, entry, 'error', sendErrorCode(error));
  }
}

/** Loads the newest page of the versions of an entry of the board at `location` into the reader. */
export async function loadHistory(context: TasksContext, location: BoardLocation, entry: string): Promise<void> {
  await readHistory(context, location, entry, null);
}

/** Reads the next older page of a history; nothing while it loads or when it is read to its start. */
export async function loadHistoryPage(context: TasksContext, location: BoardLocation, entry: string): Promise<void> {
  const load = context.store.getState().whiteboards.histories[entry];
  if (load === undefined || load.status === 'loading' || load.next === null) return;
  await readHistory(context, location, entry, load.next);
}

/** The server paging of a history for its windowed list. */
export function historyPaging(
  context: TasksContext,
  location: BoardLocation,
  entry: string,
  load: Load | null,
): ListPaging {
  return {
    hasMore: load !== null && load.next !== null,
    loading: load?.status === 'loading',
    loadMore: () => void loadHistoryPage(context, location, entry),
  };
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
