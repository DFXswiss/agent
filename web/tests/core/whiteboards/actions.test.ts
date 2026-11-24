// Board actions against the scriptable client with a real reader: loading rows and events (the first page, older pages
// on demand, task filter, once per generation, errors), the history page by page (and on a hub without paging), the
// `ListPaging` of both, create with and without notify, edit and delete with `base_event_id`, the
// rights check before sealing, text limits, missing keys, kept events for retries.
import { describe, expect, it } from 'vitest';
import { openEvent } from '../../../src/core/crypto/event';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { epochKey, ORG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import { setIdentity } from '../../../src/core/store/slices/me';
import { blockOrg, orgEntry } from '../../../src/core/store/slices/orgs';
import {
  boardPaging,
  createEntry,
  deleteEntry,
  editEntry,
  HISTORY_PAGE,
  historyPaging,
  historyTarget,
  listTarget,
  loadBoard,
  loadBoardPage,
  loadHistory,
  loadHistoryPage,
} from '../../../src/core/whiteboards/actions';
import { boardKey, orgBoard, taskBoard, wgBoard } from '../../../src/core/whiteboards/location';
import { projectBoard, TEXT_MAX } from '../../../src/core/whiteboards/project';
import { keepWrite, setBoardLoading, setHistoryLoad } from '../../../src/core/store/slices/whiteboards';
import { boardEvent, boardWorld, entryId } from './world';

const TASK = '0190f000-0000-7000-8000-00000000000c';
const empty = { events: [], next_before: null, head: 0 };

const row = (entry: string, org: string) => ({
  entry_id: entry,
  org_id: org,
  level: 'org',
  wg_id: null,
  task_id: null,
  author_id: org,
  created_at: '2026-11-21T12:00:00.000Z',
  updated_at: '2026-11-21T12:00:00.000Z',
  deleted: false,
  latest_event: { scope_id: org, scope_seq: 1 },
});

/** Accepts `POST /v1/events`, numbering per scope, and records the bodies. */
function acceptEvents(w: Awaited<ReturnType<typeof boardWorld>>) {
  const bodies: { events: { event: { header: Record<string, unknown> }; notify?: boolean }[] }[] = [];
  let seq = 0;
  w.client.on('POST /v1/events', (body) => {
    bodies.push(body as never);
    seq += 1;
    return { results: [{ id: 'x', scope_id: 'x', scope_seq: seq, replayed: false }] };
  });
  return bodies;
}

describe('listTarget', () => {
  it('names the level and the board', () => {
    const target = new URL(listTarget(taskBoard('o', 'w', 't'), 'cursor'), 'https://h');
    expect(Object.fromEntries(target.searchParams)).toEqual({
      org: 'o',
      level: 'task',
      wg: 'w',
      task: 't',
      limit: '200',
      before: 'cursor',
    });
    expect(listTarget(orgBoard('o'), null)).toBe('/v1/whiteboards?org=o&level=org&limit=200');
  });
});

describe('loadBoard', () => {
  it('pulls the board events once, then lists the first page of rows and older pages on demand', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const a = await entryId('a');
    const stored = await boardEvent(w, board, 'whiteboard.entry', a, { level: 'org', text: 'Hello' });
    w.client.on('GET /v1/whiteboards', (_body, target) =>
      target.includes('before=')
        ? { entries: [row(a, w.org), row('b', w.org)], next_before: null }
        : { entries: [row(a, w.org)], next_before: 'c1' },
    );
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({
      events: [{ scope_seq: 4, received_at: null, event: stored }],
      next_before: null,
      head: 4,
    }));
    await loadBoard(w.context, board);
    const key = boardKey(board);
    const state = () => w.store.getState().whiteboards.boards[key]!;
    expect(state()).toMatchObject({ status: 'ready', code: null, checked: 4, next: 'c1' });
    expect(state().rows.map((r) => r.entry_id)).toEqual([a]);
    expect(w.client.calls.map((call) => call.target.split('?')[0])).toEqual([
      `/v1/scopes/${w.org}/events`,
      '/v1/whiteboards',
    ]);
    expect(boardPaging(w.context, board, state())).toMatchObject({ hasMore: true, loading: false });
    // the next page on demand: appended without repeating a listed row, then nothing more
    boardPaging(w.context, board, state()).loadMore();
    expect(state().status).toBe('loading');
    await expect.poll(() => state().status).toBe('ready');
    expect(state().rows.map((r) => r.entry_id)).toEqual([a, 'b']);
    expect(state()).toMatchObject({ checked: 4, next: null });
    expect(w.client.of('GET /v1/whiteboards')[1]!.target).toContain('before=c1');
    expect(boardPaging(w.context, board, state()).hasMore).toBe(false);
    await loadBoardPage(w.context, board);
    expect(w.client.of('GET /v1/whiteboards')).toHaveLength(2);
    const pull = new URL(w.client.of(`GET /v1/scopes/${w.org}/events`)[0]!.target, 'https://h');
    expect(pull.searchParams.get('kinds')).toBe('whiteboard.entry,whiteboard.edit,whiteboard.delete');
    expect(pull.searchParams.has('task')).toBe(false);
    const view = projectBoard(w.store.getState().events[w.org], board, [], null);
    expect((view.items[0] as { text: string }).text).toBe('Hello');
    // a reload lists the first page again, the events were pulled once
    await loadBoard(w.context, board);
    expect(w.client.of('GET /v1/whiteboards')).toHaveLength(3);
    expect(state().rows.map((r) => r.entry_id)).toEqual([a]);
    expect(w.client.of(`GET /v1/scopes/${w.org}/events`)).toHaveLength(1);
  });

  it('drops answers that arrive after a sign-out', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
    w.client.on(`GET /v1/scopes/${w.wg}/events`, () => empty);
    w.client.on('GET /v1/whiteboards', () => {
      w.store.reset();
      return { entries: [], next_before: null };
    });
    await loadBoard(w.context, board);
    expect(w.store.getState().whiteboards.boards).toEqual({});
    w.client.on('GET /v1/whiteboards', () => {
      w.store.reset();
      throw new HubError('unavailable', 503);
    });
    await loadBoard(w.context, wgBoard(w.org, w.wg));
    expect(w.store.getState().whiteboards.boards).toEqual({});
    w.client.on('GET /v1/whiteboards/e1/history', () => {
      w.store.reset();
      return { events: [] };
    });
    await loadHistory(w.context, board, 'e1');
    expect(w.store.getState().whiteboards.histories).toEqual({});
    // only the (empty) board pulls were read, nothing of the history
    expect(w.ingested.flatMap((item) => item.items)).toEqual([]);
    w.client.on('GET /v1/whiteboards/e2/history', () => {
      w.store.reset();
      throw new HubError('unavailable', 503);
    });
    await loadHistory(w.context, board, 'e2');
    expect(w.store.getState().whiteboards.histories).toEqual({});
  });

  it('keeps nothing of a write whose answer arrives after a sign-out', async () => {
    const w = await boardWorld();
    w.client.on('POST /v1/events', () => {
      w.store.reset();
      return { results: [{ scope_seq: 1 }] };
    });
    await expect(createEntry(w.context, orgBoard(w.org), 'x', false)).rejects.toMatchObject({ code: 'unauthorized' });
    expect(w.store.getState().whiteboards.kept).toEqual({});
    expect(w.ingested).toEqual([]);
  });

  it('filters the task board by task and records failures', async () => {
    const w = await boardWorld();
    const board = taskBoard(w.org, w.wg, TASK);
    w.client.on('GET /v1/whiteboards', () => ({ entries: [], next_before: null }));
    w.client.on(`GET /v1/scopes/${w.wg}/events`, () => empty);
    await loadBoard(w.context, board);
    const pull = new URL(w.client.of(`GET /v1/scopes/${w.wg}/events`)[0]!.target, 'https://h');
    expect(pull.searchParams.get('task')).toBe(TASK);
    w.client.on('GET /v1/whiteboards', () => ({ entries: [{ entry_id: 1 }], next_before: null }));
    await loadBoard(w.context, board);
    expect(w.store.getState().whiteboards.boards[boardKey(board)]).toMatchObject({
      status: 'error',
      code: 'unavailable',
    });
    w.client.on('GET /v1/whiteboards', () => 'nonsense');
    await loadBoard(w.context, wgBoard(w.org, w.wg));
    expect(w.store.getState().whiteboards.boards[boardKey(wgBoard(w.org, w.wg))]!.code).toBe('unavailable');
    w.client.routes.delete('GET /v1/whiteboards');
    await loadBoard(w.context, board);
    expect(w.store.getState().whiteboards.boards[boardKey(board)]!.code).toBe('not_found');
  });

  it('reads no older page while loading, before the first page or after a reload, and records page failures', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const key = boardKey(board);
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
    // nothing loaded yet
    await loadBoardPage(w.context, board);
    expect(w.client.calls).toEqual([]);
    expect(boardPaging(w.context, board, null)).toMatchObject({ hasMore: false, loading: false });
    let answer: () => unknown = () => ({ entries: [row('a', w.org)], next_before: 'c1' });
    w.client.on('GET /v1/whiteboards', () => answer());
    await loadBoard(w.context, board);
    // while a page loads, a second request is not sent
    setBoardLoading(w.store, key);
    await loadBoardPage(w.context, board);
    expect(w.client.of('GET /v1/whiteboards')).toHaveLength(1);
    expect(boardPaging(w.context, board, w.store.getState().whiteboards.boards[key]!).loading).toBe(true);
    await loadBoard(w.context, board);
    // a reload while the page is on its way: the late page is dropped
    answer = async () => {
      answer = () => ({ entries: [row('a', w.org)], next_before: 'c2' });
      await loadBoard(w.context, board);
      return { entries: [row('late', w.org)], next_before: null };
    };
    await loadBoardPage(w.context, board);
    expect(w.store.getState().whiteboards.boards[key]!.next).toBe('c2');
    expect(w.store.getState().whiteboards.boards[key]!.rows.map((r) => r.entry_id)).toEqual(['a']);
    // a failed page keeps the rows; a sign-out drops a late page and a late failure
    answer = () => {
      throw new HubError('unavailable', 503);
    };
    await loadBoardPage(w.context, board);
    expect(w.store.getState().whiteboards.boards[key]).toMatchObject({
      status: 'error',
      code: 'unavailable',
      next: 'c2',
    });
    answer = () => {
      w.store.reset();
      return { entries: [], next_before: null };
    };
    await loadBoardPage(w.context, board);
    expect(w.store.getState().whiteboards.boards).toEqual({});
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
    answer = () => ({ entries: [], next_before: 'c3' });
    await loadBoard(w.context, board);
    answer = () => {
      w.store.reset();
      throw new HubError('unavailable', 503);
    };
    await loadBoardPage(w.context, board);
    expect(w.store.getState().whiteboards.boards).toEqual({});
  });

  it('keeps a running load as it is', async () => {
    const w = await boardWorld();
    const key = boardKey(orgBoard(w.org));
    setBoardLoading(w.store, key);
    const state = w.store.getState().whiteboards;
    setBoardLoading(w.store, key);
    expect(w.store.getState().whiteboards).toBe(state);
  });
});

describe('loadHistory', () => {
  it('reads every version into the store', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const a = await entryId('h');
    const first = await boardEvent(w, board, 'whiteboard.entry', a, { level: 'org', text: 'v1' });
    const second = await boardEvent(w, board, 'whiteboard.edit', a, {
      text: 'v2',
      base_event_id: '0190f000-0000-7000-8000-0000000000b0',
    });
    w.client.on(`GET /v1/whiteboards/${a}/history`, () => ({
      events: [
        { scope_seq: 3, received_at: null, event: first },
        { scope_seq: 7, received_at: '2026-11-21T12:00:00.000Z', event: second },
      ],
    }));
    await loadHistory(w.context, board, a);
    expect(w.store.getState().whiteboards.histories[a]).toEqual({ status: 'ready', code: null, next: null });
    const entry = projectBoard(w.store.getState().events[w.org], board, [], null).entries.get(a)!;
    expect(entry.versions.map((v) => v.text)).toEqual(['v1', 'v2']);
    setHistoryLoad(w.store, a, 'ready');
    expect(w.store.getState().whiteboards.histories[a]).toEqual({ status: 'ready', code: null, next: null });
    // a second load reads only what the reader has not settled yet (an unverifiable event stays as well)
    w.store.update('events', (events) => {
      const entries = new Map(events[w.org]!.entries);
      entries.set(7, { ...entries.get(7)!, status: 'locked' });
      entries.set(9, { ...entries.get(7)!, scopeSeq: 9, status: 'unverifiable', code: 'signature_invalid' });
      return { ...events, [w.org]: { entries } };
    });
    w.client.on(`GET /v1/whiteboards/${a}/history`, () => ({
      events: [
        { scope_seq: 3, received_at: null, event: first },
        { scope_seq: 7, received_at: null, event: second },
        { scope_seq: 9, received_at: null, event: second },
      ],
    }));
    const before = w.ingested.length;
    await loadHistory(w.context, board, a);
    expect(w.ingested.slice(before).flatMap((item) => item.items.map((stored) => stored.scope_seq))).toEqual([7]);
  });

  it('records a malformed or failed answer', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    w.client.on('GET /v1/whiteboards/e1/history', () => ({ events: [{ scope_seq: 'x' }] }));
    await loadHistory(w.context, board, 'e1');
    expect(w.store.getState().whiteboards.histories.e1).toEqual({ status: 'error', code: 'unavailable', next: null });
    w.client.on('GET /v1/whiteboards/e2/history', () => null);
    await loadHistory(w.context, board, 'e2');
    expect(w.store.getState().whiteboards.histories.e2!.code).toBe('unavailable');
    await loadHistory(w.context, board, 'e3');
    expect(w.store.getState().whiteboards.histories.e3!.code).toBe('not_found');
  });
  it('reads the history newest first, page by page on demand', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const a = await entryId('p');
    const first = await boardEvent(w, board, 'whiteboard.entry', a, { level: 'org', text: 'v1' });
    const edit = (text: string) =>
      boardEvent(w, board, 'whiteboard.edit', a, { text, base_event_id: '0190f000-0000-7000-8000-0000000000b0' });
    const [second, third] = [await edit('v2'), await edit('v3')];
    w.client.on(`GET /v1/whiteboards/${a}/history`, (_body, target) =>
      new URL(target, 'https://h').searchParams.get('before') === '5'
        ? { events: [{ scope_seq: 2, received_at: null, event: first }], next_before: null }
        : {
            events: [
              { scope_seq: 8, received_at: null, event: third },
              { scope_seq: 5, received_at: null, event: second },
            ],
            next_before: 5,
          },
    );
    const history = () => w.store.getState().whiteboards.histories[a] ?? null;
    expect(historyPaging(w.context, board, a, history())).toMatchObject({ hasMore: false, loading: false });
    await loadHistoryPage(w.context, board, a);
    expect(w.client.calls).toEqual([]);
    await loadHistory(w.context, board, a);
    expect(w.client.calls[0]!.target).toBe(`/v1/whiteboards/${a}/history?limit=${HISTORY_PAGE}`);
    expect(history()).toEqual({ status: 'ready', code: null, next: 5 });
    // the page is read in ascending order
    expect(w.ingested.at(-1)!.items.map((item) => item.scope_seq)).toEqual([5, 8]);
    expect(historyPaging(w.context, board, a, history())).toMatchObject({ hasMore: true, loading: false });
    historyPaging(w.context, board, a, history()).loadMore();
    expect(historyPaging(w.context, board, a, history()).loading).toBe(true);
    // while a page loads, no second request
    await loadHistoryPage(w.context, board, a);
    await expect.poll(() => history()!.status).toBe('ready');
    expect(w.client.calls).toHaveLength(2);
    expect(w.client.calls[1]!.target).toBe(`/v1/whiteboards/${a}/history?limit=${HISTORY_PAGE}&before=5`);
    expect(history()!.next).toBeNull();
    await loadHistoryPage(w.context, board, a);
    expect(w.client.calls).toHaveLength(2);
    // a failed older page keeps its position for a retry
    setHistoryLoad(w.store, a, 'ready', null, 5);
    w.client.on(`GET /v1/whiteboards/${a}/history`, () => {
      throw new HubError('rate_limited', 429);
    });
    await loadHistoryPage(w.context, board, a);
    expect(history()).toEqual({ status: 'error', code: 'rate_limited', next: 5 });
  });

  it('asks a hub without paging once more without it and remembers that hub', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const a = await entryId('o');
    const first = await boardEvent(w, board, 'whiteboard.entry', a, { level: 'org', text: 'v1' });
    w.client.on(`GET /v1/whiteboards/${a}/history`, (_body, target) => {
      if (target.includes('?')) throw new HubError('request_invalid', 400);
      return { events: [{ scope_seq: 2, received_at: null, event: first }], truncated: true };
    });
    await loadHistory(w.context, board, a);
    expect(w.client.calls.map((call) => call.target)).toEqual([
      `/v1/whiteboards/${a}/history?limit=${HISTORY_PAGE}`,
      `/v1/whiteboards/${a}/history`,
    ]);
    expect(w.store.getState().whiteboards.histories[a]).toEqual({ status: 'ready', code: null, next: null });
    await loadHistory(w.context, board, a);
    expect(w.client.calls.map((call) => call.target).slice(2)).toEqual([`/v1/whiteboards/${a}/history`]);
    expect(historyTarget('x y', 3)).toBe('/v1/whiteboards/x%20y/history?limit=50&before=3');
    // a failure that is not a hub answer
    w.client.on(`GET /v1/whiteboards/${a}/history`, () => {
      throw new TypeError('offline');
    });
    await loadHistory(w.context, board, a);
    expect(w.store.getState().whiteboards.histories[a]).toMatchObject({ status: 'error', code: 'unavailable' });
    const fresh = await boardWorld();
    fresh.client.on('GET /v1/whiteboards/e/history', () => {
      throw new TypeError('offline');
    });
    await loadHistory(fresh.context, orgBoard(fresh.org), 'e');
    expect(fresh.client.calls).toHaveLength(1);
  });
});

describe('writes', () => {
  it('creates an encrypted entry with notify and reads it back', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const bodies = acceptEvents(w);
    const id = await createEntry(w.context, board, '# Plan\nShip it', true);
    expect(bodies).toHaveLength(1);
    const item = bodies[0]!.events[0]!;
    expect(item.notify).toBe(true);
    expect(item.event.header).toMatchObject({
      kind: 'whiteboard.entry',
      scope: 'org',
      scope_id: w.org,
      wg: '',
      task: '',
      ref: id,
      author: w.ada.id,
      lh: w.world.log!.head.seq,
    });
    // the hub sees only ciphertext
    expect(JSON.stringify(item)).not.toContain('Ship it');
    const key = orgEntry(w.store.getState().orgs, w.org).keys.get(epochKey(ORG, w.org, 1))!;
    const plain = fromUtf8(await openEvent(key, item.event, b64uDecode(w.ada.identity.public().sig)));
    expect(JSON.parse(plain)).toEqual({ level: 'org', text: '# Plan\nShip it' });
    const view = projectBoard(w.store.getState().events[w.org], board, [], null);
    expect(view.entries.get(id)!.text).toBe('# Plan\nShip it');
    expect(w.store.getState().whiteboards.kept).toEqual({});
  });

  it('edits and deletes with the newest event as base, without notify', async () => {
    const w = await boardWorld();
    const board = taskBoard(w.org, w.wg, TASK);
    const bodies = acceptEvents(w);
    const id = await createEntry(w.context, board, 'Draft', false);
    expect(bodies[0]!.events[0]!.notify).toBeUndefined();
    expect(bodies[0]!.events[0]!.event.header).toMatchObject({ scope: 'wg', wg: w.wg, task: TASK });
    const entry = () => projectBoard(w.store.getState().events[w.wg], board, [], null).entries.get(id)!;
    const first = entry().latestEventId;
    await editEntry(w.context, board, entry(), 'Final');
    expect(entry().text).toBe('Final');
    await deleteEntry(w.context, board, entry());
    expect(entry().deleted).toBe(true);
    expect(entry().versions).toHaveLength(3);
    const edit = bodies[1]!.events[0]!.event;
    expect(edit.header).toMatchObject({ kind: 'whiteboard.edit', ref: id });
    expect(bodies[1]!.events[0]!.notify).toBeUndefined();
    const key = orgEntry(w.store.getState().orgs, w.org).keys.get(epochKey('wg', w.wg, 1))!;
    const plain = fromUtf8(await openEvent(key, edit, b64uDecode(w.ada.identity.public().sig)));
    expect(JSON.parse(plain)).toEqual({ text: 'Final', base_event_id: first });
  });

  it('refuses without the right, without an identity, in a blocked org and before the log', async () => {
    const reader = await boardWorld({ member: true });
    await expect(createEntry(reader.context, orgBoard(reader.org), 'x', false)).rejects.toMatchObject({
      code: 'forbidden',
    });
    await expect(createEntry(reader.context, wgBoard(reader.org, reader.research), 'x', false)).rejects.toMatchObject({
      code: 'forbidden',
    });
    expect(reader.client.calls).toEqual([]);
    const w = await boardWorld();
    blockOrg(w.store, w.org, { code: 'log_fork', evidence: {} } as never);
    await expect(createEntry(w.context, orgBoard(w.org), 'x', false)).rejects.toMatchObject({ code: 'log_fork' });
    const fresh = await boardWorld();
    fresh.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [fresh.org]: { ...orgEntry(orgs, fresh.org), log: null } },
    }));
    await expect(createEntry(fresh.context, orgBoard(fresh.org), 'x', false)).rejects.toMatchObject({
      code: 'log_empty',
    });
    setIdentity(fresh.store, null);
    await expect(createEntry(fresh.context, orgBoard(fresh.org), 'x', false)).rejects.toMatchObject({
      code: 'unauthorized',
    });
  });

  it('checks the text length', async () => {
    const w = await boardWorld();
    await expect(createEntry(w.context, orgBoard(w.org), '', false)).rejects.toMatchObject({
      code: 'request_invalid',
    });
    const long = 'x'.repeat(TEXT_MAX + 1);
    await expect(editEntry(w.context, orgBoard(w.org), { id: 'e', latestEventId: 'l' }, long)).rejects.toMatchObject({
      code: 'size_exceeded',
    });
  });

  it('asks the log job once for a missing key, then refuses with epoch_stale', async () => {
    const w = await boardWorld();
    w.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [w.org]: { ...orgEntry(orgs, w.org), keys: new Map() } },
    }));
    await expect(createEntry(w.context, orgBoard(w.org), 'x', false)).rejects.toMatchObject({ code: 'epoch_stale' });
    expect(w.context.refreshLog).toHaveBeenCalledWith(w.org);
    const keys = w.world.held.get(w.ada.id)!;
    w.context.refreshLog.mockImplementation(async () => {
      w.store.update('orgs', (orgs) => ({
        ...orgs,
        byId: { ...orgs.byId, [w.org]: { ...orgEntry(orgs, w.org), keys } },
      }));
      return true;
    });
    acceptEvents(w);
    expect(await createEntry(w.context, orgBoard(w.org), 'x', false)).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('keeps the sealed event after a retryable failure and sends it again for the same request', async () => {
    const w = await boardWorld();
    const board = wgBoard(w.org, w.wg);
    let fail: HubError | null = new HubError('unavailable', 503);
    const sent: unknown[] = [];
    w.client.on('POST /v1/events', (body) => {
      sent.push(body);
      if (fail !== null) throw fail;
      return { results: [{ scope_seq: 1 }] };
    });
    await expect(createEntry(w.context, board, 'Same', true)).rejects.toMatchObject({ code: 'unavailable' });
    expect(Object.keys(w.store.getState().whiteboards.kept)).toHaveLength(1);
    fail = null;
    const id = await createEntry(w.context, board, 'Same', true);
    expect(sent[1]).toEqual(sent[0]);
    expect((sent[0] as { events: { event: { header: { ref: string } } }[] }).events[0]!.event.header.ref).toBe(id);
    expect(w.store.getState().whiteboards.kept).toEqual({});
    // a different request seals anew; a final failure forgets the event
    fail = new HubError('forbidden', 403);
    await expect(createEntry(w.context, board, 'Other', false)).rejects.toMatchObject({ code: 'forbidden' });
    expect(w.store.getState().whiteboards.kept).toEqual({});
    keepWrite(w.store, 'nothing', null);
    expect(w.store.getState().whiteboards.kept).toEqual({});
  });

  it('refuses an answer without a position', async () => {
    const w = await boardWorld();
    w.client.on('POST /v1/events', () => ({ results: [] }));
    await expect(createEntry(w.context, orgBoard(w.org), 'x', false)).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on('POST /v1/events', () => null);
    await expect(createEntry(w.context, orgBoard(w.org), 'x', false)).rejects.toMatchObject({ code: 'unavailable' });
  });
});
