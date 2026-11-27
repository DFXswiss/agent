// U-WEB-ui-tasks (overview, KOR-32): the readable workgroups from the verified log, the k-way merge of the workgroup
// lists by latest activity with the cut-off of the lists that have further pages, the browser-side filters ("only
// mine", title search), the stable selector, and the loads (first pages, refresh, further pages of the binding list).
import { describe, expect, it } from 'vitest';
import { HubError } from '../../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { setLog } from '../../../src/core/store/slices/orgs';
import { listLoading, listPage, putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import { PAGE_SIZE } from '../../../src/core/tasks/list';
import {
  clientFiltered,
  loadOverview,
  loadOverviewMore,
  overview,
  type OverviewQuery,
  readableWorkgroups,
  searchText,
  selectOverview,
} from '../../../src/core/tasks/overview';
import { signCert } from '../../../src/core/crypto/agent';
import { actor, idOf } from '../log/world';
import { taskRow, taskWorld, type TaskWorld } from './world';

const at = (second: number) =>
  `2026-11-21T12:${String(Math.floor(second / 60)).padStart(2, '0')}:${String(second % 60).padStart(2, '0')}.000Z`;

const row = (t: TaskWorld, id: string, wg: string, second: number, overrides: Record<string, unknown> = {}) =>
  taskRow(t, { task_id: id, wg_id: wg, created_at: at(0), last_activity_at: at(second), ...overrides }) as TaskRow;

const query = (t: TaskWorld, wgs: string[], overrides: Partial<OverviewQuery> = {}): OverviewQuery => ({
  org: t.org,
  wgs,
  state: null,
  mine: null,
  search: '',
  ...overrides,
});

const ids = (rows: readonly TaskRow[]) => rows.map((item) => item.task_id);

function meta(t: TaskWorld, seq: number, wg: string, task: string, author: string, title: string): EventEntry {
  return {
    org: t.org,
    scopeId: wg,
    scopeSeq: seq,
    receivedAt: null,
    event: { header: { id: `m${seq}` } },
    status: 'verified',
    code: null,
    header: { id: `m${seq}`, kind: 'task.meta', task, author, ts: at(0) },
    body: { title },
  };
}

describe('readable workgroups', () => {
  it('lists the workgroups the user can read from the verified log, never the others', async () => {
    const t = await taskWorld({ member: true });
    const [readable, hidden] = [await idOf('readable wg'), await idOf('hidden wg')];
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: readable, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: readable, access: 'read' },
    ]);
    await t.world.batch(t.ada, [{ type: 'wg_create', wg: hidden, epoch: 1 }]);
    setLog(t.store, t.org, t.world.log!);
    const state = t.world.state;
    expect(readableWorkgroups(state, t.me.id).sort()).toEqual([t.wg, readable].sort());
    expect(readableWorkgroups(state, t.me.id)[0]).toBe(t.wg);
    expect(readableWorkgroups(state, t.ada.id)).toContain(hidden);
  });
});

describe('merge', () => {
  it('waits for every first page, then merges by latest activity with the task ID as tie-breaker', async () => {
    const t = await taskWorld();
    const q = query(t, ['w1', 'w2']);
    expect(overview(t.store.getState(), q)).toMatchObject({ status: 'loading', rows: [], busy: false, code: null });
    listPage(t.store, {
      wg: 'w1',
      state: null,
      rows: [row(t, 'a', 'w1', 5), row(t, 'c', 'w1', 1)],
      nextBefore: null,
      first: true,
    });
    listLoading(t.store, 'w2', null);
    expect(overview(t.store.getState(), q)).toMatchObject({ status: 'loading', busy: true });
    listPage(t.store, {
      wg: 'w2',
      state: null,
      rows: [row(t, 'b', 'w2', 5), row(t, 'd', 'w2', 3)],
      nextBefore: null,
      first: true,
    });
    const view = overview(t.store.getState(), q);
    expect(view).toMatchObject({ status: 'ready', binding: null, busy: false, failed: [] });
    expect(ids(view.rows)).toEqual(['b', 'a', 'd', 'c']);
    expect(view.merged).toEqual(view.rows);
    expect(searchText('  ÄBC ')).toBe('äbc');
  });

  it('shows rows only down to the newest last row of the lists with a further page', async () => {
    const t = await taskWorld();
    const q = query(t, ['w1', 'w2', 'w3']);
    listPage(t.store, {
      wg: 'w1',
      state: null,
      rows: [row(t, 'a1', 'w1', 50), row(t, 'a2', 'w1', 40)],
      nextBefore: 'c1',
      first: true,
    });
    listPage(t.store, {
      wg: 'w2',
      state: null,
      rows: [row(t, 'b1', 'w2', 45), row(t, 'b2', 'w2', 30)],
      nextBefore: 'c2',
      first: true,
    });
    listPage(t.store, {
      wg: 'w3',
      state: null,
      rows: [row(t, 'c1', 'w3', 35), row(t, 'c2', 'w3', 10)],
      nextBefore: null,
      first: true,
    });
    // w1 binds: its unloaded rows are older than a2 (40), w2's older than b2 (30)
    let view = overview(t.store.getState(), q);
    expect(ids(view.rows)).toEqual(['a1', 'b1', 'a2']);
    expect(view.binding).toBe('w1');
    // a list with a further page but no rows binds everything
    listPage(t.store, { wg: 'w3', state: null, rows: [], nextBefore: 'c3', first: true });
    view = overview(t.store.getState(), q);
    expect(view).toMatchObject({ rows: [], binding: 'w3' });
    listPage(t.store, { wg: 'w3', state: null, rows: [], nextBefore: null, first: true });
    // a list loading its next page keeps its rows; a failed list is reported and keeps what it had
    listLoading(t.store, 'w1', null);
    view = overview(t.store.getState(), q);
    expect(view).toMatchObject({ status: 'ready', busy: true, binding: 'w1' });
    t.store.update('tasks', (state) => ({
      ...state,
      lists: { ...state.lists, 'w2 ': { ...state.lists['w2 ']!, status: 'error', code: 'unavailable' } },
    }));
    view = overview(t.store.getState(), q);
    expect(view).toMatchObject({ failed: ['w2'], code: 'unavailable' });
    expect(ids(view.rows)).toEqual(['a1', 'b1', 'a2']);
  });

  it('filters by owner (signed author) and title in the browser', async () => {
    const t = await taskWorld({ member: true });
    listPage(t.store, {
      wg: t.wg,
      state: null,
      rows: [row(t, 't1', t.wg, 3), row(t, 't2', t.wg, 2), row(t, 't3', t.wg, 1)],
      nextBefore: null,
      first: true,
    });
    putEvents(t.store, t.wg, [
      meta(t, 1, t.wg, 't1', t.me.id, 'Quarterly Report'),
      meta(t, 2, t.wg, 't2', t.ada.id, 'Report draft'),
    ]);
    const view = (overrides: Partial<OverviewQuery>) =>
      ids(overview(t.store.getState(), query(t, [t.wg], overrides)).rows);
    expect(view({ mine: t.me.id })).toEqual(['t1']);
    // a task of the user's agent is the user's own (ADR-082)
    const bot = await actor('overview bot');
    const pub = bot.identity.public();
    const cert = await signCert(t.me.identity.sig.privateKey, { agent: bot.id, pub, user: t.me.id, gen: 1 });
    t.world.add(bot);
    await t.world.batch(bot, [{ type: 'agent_add', subject: bot.id, user: t.me.id, pub, gen: 1, cert }], {
      keys: t.world.ring(t.ada.id),
    });
    setLog(t.store, t.org, t.world.log!);
    putEvents(t.store, t.wg, [meta(t, 3, t.wg, 't3', bot.id, 'Agent notes')]);
    expect(view({ mine: t.me.id })).toEqual(['t1', 't3']);
    expect(view({ mine: t.ada.id })).toEqual(['t2']);
    expect(view({ search: ' report ' })).toEqual(['t1', 't2']);
    expect(view({ search: 'QUARTER', mine: t.me.id })).toEqual(['t1']);
    expect(view({ search: 'draft', mine: t.me.id })).toEqual([]);
    // a task without a decrypted title does not match a search
    expect(view({ search: 't3' })).toEqual([]);
    expect(overview(t.store.getState(), query(t, [t.wg], { search: 'x' })).merged).toHaveLength(3);
  });

  it('filters by the sealed GitHub organisation and repository in the browser (ADR-102)', async () => {
    const t = await taskWorld();
    listPage(t.store, {
      wg: t.wg,
      state: null,
      rows: [row(t, 't1', t.wg, 3), row(t, 't2', t.wg, 2), row(t, 't3', t.wg, 1)],
      nextBefore: null,
      first: true,
    });
    const sealed = (seq: number, task: string, body: Record<string, unknown>): EventEntry => ({
      ...meta(t, seq, t.wg, task, t.me.id, 'x'),
      body: { title: 'x', ...body },
    });
    putEvents(t.store, t.wg, [
      sealed(1, 't1', { github_org: 'DFXswiss', github_repo: 'DFXswiss/agent' }),
      sealed(2, 't2', { github_org: 'acme' }),
    ]);
    const view = (overrides: Partial<OverviewQuery>) =>
      ids(overview(t.store.getState(), query(t, [t.wg], overrides)).rows);
    expect(view({ githubOrg: ' dfxswiss ' })).toEqual(['t1']);
    expect(view({ githubRepo: 'dfxswiss/AGENT' })).toEqual(['t1']);
    expect(view({ githubOrg: 'acme', githubRepo: 'dfxswiss/agent' })).toEqual([]);
    expect(view({ githubOrg: 'acme' })).toEqual(['t2']);
    expect(view({ githubOrg: '', githubRepo: '' })).toEqual(['t1', 't2', 't3']);
    expect(clientFiltered(query(t, []))).toBe(false);
    expect(clientFiltered(query(t, [], { githubOrg: ' ' }))).toBe(false);
    expect(clientFiltered(query(t, [], { githubOrg: 'a' }))).toBe(true);
    expect(clientFiltered(query(t, [], { githubRepo: 'a/b' }))).toBe(true);
    expect(clientFiltered(query(t, [], { search: 'a' }))).toBe(true);
    expect(clientFiltered(query(t, [], { mine: 'u' }))).toBe(true);
  });

  it('selects the same object while nothing shown changed', async () => {
    const t = await taskWorld();
    const select = selectOverview(query(t, ['w1']));
    const first = select(t.store.getState());
    expect(select(t.store.getState())).toBe(first);
    listPage(t.store, { wg: 'w1', state: null, rows: [row(t, 'a', 'w1', 1)], nextBefore: null, first: true });
    const loaded = select(t.store.getState());
    expect(loaded).not.toBe(first);
    t.store.update('events', (state) => ({ ...state }));
    expect(select(t.store.getState())).toBe(loaded);
    // each part of the view counts
    putTasks(t.store, [row(t, 'a', 'w1', 2)]);
    const moved = select(t.store.getState());
    expect(moved).not.toBe(loaded);
    listLoading(t.store, 'w1', null);
    const busy = select(t.store.getState());
    expect(busy).not.toBe(moved);
    t.store.update('tasks', (state) => ({
      ...state,
      lists: { ...state.lists, 'w1 ': { ...state.lists['w1 ']!, status: 'error', code: 'unavailable' } },
    }));
    const failed = select(t.store.getState());
    expect(failed).not.toBe(busy);
    t.store.update('tasks', (state) => ({
      ...state,
      lists: { ...state.lists, 'w1 ': { ...state.lists['w1 ']!, status: 'error', code: 'forbidden' } },
    }));
    expect(select(t.store.getState())).not.toBe(failed);
    const filtered = selectOverview(query(t, ['w1'], { search: 'nothing' }));
    const none = filtered(t.store.getState());
    expect(none.rows).toEqual([]);
    expect(filtered(t.store.getState())).toBe(none);
    const twice = selectOverview(query(t, ['w1', 'w2']));
    const waiting = twice(t.store.getState());
    listPage(t.store, { wg: 'w2', state: null, rows: [], nextBefore: 'c', first: true });
    expect(twice(t.store.getState())).not.toBe(waiting);
    listPage(t.store, { wg: 'w2', state: null, rows: [], nextBefore: 'd', first: true });
    const bound = twice(t.store.getState());
    listPage(t.store, { wg: 'w2', state: null, rows: [], nextBefore: null, first: true });
    expect(twice(t.store.getState())).not.toBe(bound);
  });
});

describe('loads', () => {
  it('loads the first page of every workgroup and only of those', async () => {
    const t = await taskWorld();
    for (const wg of ['w1', 'w2']) {
      t.client.on(`GET /v1/workgroups/${wg}/tasks`, () => ({ tasks: [row(t, `${wg}-a`, wg, 1)], next_before: null }));
    }
    await loadOverview(t.context, ['w1', 'w2'], 'working');
    expect(t.client.calls.map((call) => call.target.split('?')[0]).sort()).toEqual([
      '/v1/workgroups/w1/tasks',
      '/v1/workgroups/w2/tasks',
    ]);
    expect(
      t.client.calls.every((call) => call.target.includes('state=working') && call.target.includes('order=activity')),
    ).toBe(true);
    // a refresh loads the first pages again even while a load runs
    listLoading(t.store, 'w1', 'working');
    await loadOverview(t.context, ['w1'], 'working', true);
    expect(t.client.calls).toHaveLength(3);
  });

  it('loads pages of the binding list until a page more is shown or nothing more comes', async () => {
    const t = await taskWorld();
    const q = query(t, ['w1', 'w2']);
    // w1: PAGE_SIZE + 10 rows from 1000 s down, two pages; w2: 30 rows in between, one page of 20 then 10
    const w1 = Array.from({ length: PAGE_SIZE + 10 }, (_, index) =>
      row(t, `a${String(index).padStart(3, '0')}`, 'w1', 1000 - index * 10),
    );
    const w2 = Array.from({ length: 30 }, (_, index) =>
      row(t, `b${String(index).padStart(3, '0')}`, 'w2', 995 - index * 10),
    );
    const pages = (rows: TaskRow[], size: number) => (_body: unknown, target: string) => {
      const start = target.includes('before=') ? Number(new URLSearchParams(target.split('?')[1]).get('before')) : 0;
      const end = start + size;
      return { tasks: rows.slice(start, end), next_before: end < rows.length ? String(end) : null };
    };
    t.client.on('GET /v1/workgroups/w1/tasks', pages(w1, PAGE_SIZE));
    t.client.on('GET /v1/workgroups/w2/tasks', pages(w2, 20));
    await loadOverview(t.context, q.wgs, null);
    // w2 binds at its 20th row (805 s): 20 rows of w1 and 20 of w2 (w1's row at 810 s is above)
    expect(overview(t.store.getState(), q)).toMatchObject({ binding: 'w2' });
    expect(overview(t.store.getState(), q).merged).toHaveLength(40);
    await loadOverviewMore(t.context, q);
    const view = overview(t.store.getState(), q);
    expect(view.binding).toBeNull();
    expect(view.merged).toHaveLength(PAGE_SIZE + 40);
    expect(ids(view.merged).slice(0, 3)).toEqual(['a000', 'b000', 'a001']);
    // nothing more to load
    const calls = t.client.calls.length;
    await loadOverviewMore(t.context, q);
    expect(t.client.calls).toHaveLength(calls);
  });

  it('stops when a page brings nothing (a failure)', async () => {
    const t = await taskWorld();
    const q = query(t, ['w1']);
    t.client.on('GET /v1/workgroups/w1/tasks', (_body, target) => {
      if (target.includes('before=')) throw new HubError('unavailable', 503);
      return { tasks: [row(t, 'a', 'w1', 1)], next_before: 'next' };
    });
    await loadOverview(t.context, q.wgs, null);
    await loadOverviewMore(t.context, q);
    expect(overview(t.store.getState(), q)).toMatchObject({ failed: ['w1'], code: 'unavailable', binding: 'w1' });
    expect(t.client.calls).toHaveLength(2);
  });
});
