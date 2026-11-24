// U-WEB-ui-tasks (task list, pulls, names and titles): pages with filter and `next_before`, guards and failures,
// stable row selections, pulls page by page shared per store generation, the directory of the org scope and task
// titles from verified events only.
import { describe, expect, it } from 'vitest';
import { HubError } from '../../../src/core/net/signedFetch';
import { setLog } from '../../../src/core/store/slices/orgs';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { listLoading, taskList, type TaskRow } from '../../../src/core/store/slices/tasks';
import { selectDirectory, selectOwner, selectTitle } from '../../../src/core/tasks/directory';
import { listTarget, loadTasks, PAGE_SIZE, selectListRows, TITLE_PAGES, titles } from '../../../src/core/tasks/list';
import { eventsTarget, NEWEST, PAGE_MAX } from '../../../src/core/net/pages';
import { pullEvents, pullOnce } from '../../../src/core/tasks/pull';
import { actor } from '../log/world';
import { taskRow, taskWorld, type TaskWorld } from './world';

const page = (rows: unknown[], next: string | null = null) => ({ tasks: rows, next_before: next });

function entry(
  scope: string,
  seq: number,
  header: Record<string, unknown>,
  body: unknown,
  status: EventEntry['status'] = 'verified',
): EventEntry {
  return {
    org: 'o',
    scopeId: scope,
    scopeSeq: seq,
    receivedAt: null,
    event: { header: { id: header.id ?? `e${seq}` } },
    status,
    code: status === 'unverifiable' ? 'author_not_writer' : null,
    header: status === 'verified' ? { id: `e${seq}`, ...header } : null,
    body,
  };
}

describe('loadTasks', () => {
  it('loads the first page with the filter and the next pages with the cursor', async () => {
    const t = await taskWorld();
    const rows = [taskRow(t, { task_id: 'b', created_at: '2026-11-21T12:00:01.000Z' })];
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) =>
      target.includes('before=') ? page([taskRow(t, { task_id: 'a' })]) : page(rows, 'cursor 1'),
    );
    expect(await loadTasks(t.context, t.wg, null)).toHaveLength(1);
    expect(t.client.calls[0]!.target).toBe(listTarget(t.wg, null, null));
    expect(listTarget('w', 'working', 'c/1')).toBe(
      `/v1/workgroups/w/tasks?state=working&before=c%2F1&limit=${PAGE_SIZE}&order=activity`,
    );
    await loadTasks(t.context, t.wg, null, true);
    expect(t.client.calls[1]!.target).toContain('before=cursor+1');
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({ ids: ['b', 'a'], nextBefore: null });
    expect(await loadTasks(t.context, t.wg, null, true)).toEqual([]);
    expect(t.client.calls).toHaveLength(2);
    expect(await loadTasks(t.context, t.wg, null)).toHaveLength(1);
    expect(taskList(t.store.getState().tasks, t.wg, null).ids).toEqual(['b']);
  });

  it('refreshes the first page after a reconnect and keeps the further pages already loaded (KOR-11)', async () => {
    const t = await taskWorld();
    const at = (second: number) => `2026-11-21T12:00:${String(second).padStart(2, '0')}.000Z`;
    let first = [taskRow(t, { task_id: 'd', created_at: at(4) }), taskRow(t, { task_id: 'c', created_at: at(3) })];
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) =>
      target.includes('before=')
        ? page([taskRow(t, { task_id: 'b', created_at: at(2) }), taskRow(t, { task_id: 'a', created_at: at(1) })], 'p3')
        : page(first, 'p2'),
    );
    await loadTasks(t.context, t.wg, null);
    await loadTasks(t.context, t.wg, null, true);
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({
      ids: ['d', 'c', 'b', 'a'],
      nextBefore: 'p3',
    });
    // a new task pushes `c` out of the first page (it stays, older than the page), during another load
    first = [taskRow(t, { task_id: 'e', created_at: at(5) }), taskRow(t, { task_id: 'd', created_at: at(4) })];
    listLoading(t.store, t.wg, null);
    await loadTasks(t.context, t.wg, null, false, true);
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({
      ids: ['e', 'd', 'c', 'b', 'a'],
      nextBefore: 'p3',
      status: 'ready',
    });
    // the whole list on one page: it replaces the list; a failed refresh keeps the rows and the state
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => page([taskRow(t, { task_id: 'e', created_at: at(5) })]));
    await loadTasks(t.context, t.wg, null, false, true);
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({ ids: ['e'], nextBefore: null });
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => {
      throw new HubError('unavailable', 503);
    });
    expect(await loadTasks(t.context, t.wg, null, false, true)).toEqual([]);
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({ ids: ['e'], status: 'ready', code: null });
    // an empty first page with a cursor keeps nothing older
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => page([], 'p9'));
    await loadTasks(t.context, t.wg, null, false, true);
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({ ids: [], nextBefore: 'p9' });
  });

  it('leaves a loading list alone and records failures and malformed pages', async () => {
    const t = await taskWorld();
    listLoading(t.store, t.wg, 'idle');
    expect(await loadTasks(t.context, t.wg, 'idle')).toEqual([]);
    expect(t.client.calls).toHaveLength(0);
    expect(await loadTasks(t.context, t.wg, null)).toEqual([]);
    expect(taskList(t.store.getState().tasks, t.wg, null)).toMatchObject({ status: 'error', code: 'not_found' });
    for (const answer of [
      null,
      { tasks: [{}], next_before: null },
      { tasks: [], next_before: 3 },
      { next_before: null },
    ]) {
      t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => answer);
      await loadTasks(t.context, t.wg, null);
      expect(taskList(t.store.getState().tasks, t.wg, null).code).toBe('unavailable');
    }
  });

  it('writes nothing after a sign-out during the request', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => {
      t.store.reset();
      return page([taskRow(t)]);
    });
    expect(await loadTasks(t.context, t.wg, null)).toEqual([]);
    expect(t.store.getState().tasks.lists).toEqual({});
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => {
      t.store.reset();
      throw new HubError('unavailable', 503);
    });
    await loadTasks(t.context, t.wg, null);
    expect(t.store.getState().tasks.lists).toEqual({});
  });

  it('selects the rows of a list as a stable array', async () => {
    const t = await taskWorld();
    const select = selectListRows(t.wg, null);
    const empty = select(t.store.getState());
    expect(empty).toEqual([]);
    expect(select(t.store.getState())).toBe(empty);
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => page([taskRow(t)]));
    await loadTasks(t.context, t.wg, null);
    const rows = select(t.store.getState());
    expect(rows.map((row) => row.task_id)).toEqual([taskRow(t).task_id]);
    expect(select(t.store.getState())).toBe(rows);
    t.store.update('tasks', (state) => ({ ...state, tasks: { ...state.tasks } }));
    expect(select(t.store.getState())).not.toBe(rows);
  });
});

describe('pulls', () => {
  it('pulls the newest page of a query, or a bounded number of pages, into the reader', async () => {
    const t = await taskWorld();
    const targets: string[] = [];
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      targets.push(target);
      return target.includes(`before=${NEWEST}`)
        ? {
            events: [
              { scope_seq: 3, received_at: 'x', event: {} },
              { scope_seq: 2, event: {} },
            ],
            next_before: 2,
            head: 3,
          }
        : { events: [{ scope_seq: 1, received_at: null, event: {} }], next_before: null, head: 3 };
    });
    expect(await pullEvents(t.context, t.org, t.wg, { kinds: ['prompt'], task: 't' })).toBe(2);
    expect(targets).toEqual([eventsTarget(t.wg, { kinds: ['prompt'], task: 't' }, { before: NEWEST }, PAGE_MAX)]);
    expect(t.ingested.map((item) => item.items.map((i) => i.scope_seq))).toEqual([[2, 3]]);
    expect(await pullEvents(t.context, t.org, t.wg, { kinds: ['prompt'], pages: 5 })).toBe(3);
    expect(targets.slice(1)).toEqual([
      eventsTarget(t.wg, { kinds: ['prompt'] }, { before: NEWEST }, PAGE_MAX),
      eventsTarget(t.wg, { kinds: ['prompt'] }, { before: 2 }, PAGE_MAX),
    ]);
  });

  it('shares one pull per query and generation and forgets failed pulls', async () => {
    const t = await taskWorld();
    let calls = 0;
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      calls += 1;
      if (calls === 1) throw new HubError('unavailable', 503);
      return { events: [], next_before: null, head: 0 };
    });
    await expect(pullOnce(t.context, t.org, t.wg, { kinds: ['task.meta'] })).rejects.toThrow('unavailable');
    const first = pullOnce(t.context, t.org, t.wg, { kinds: ['task.meta'] });
    expect(pullOnce(t.context, t.org, t.wg, { kinds: ['task.meta'] })).toBe(first);
    await first;
    expect(calls).toBe(2);
    t.store.reset();
    await pullOnce(t.context, t.org, t.wg, { kinds: ['task.meta'] });
    expect(calls).toBe(3);
  });
});

describe('directory and titles', () => {
  it('reads names from the newest verified profile events of the org scope', () => {
    return taskWorld().then((t: TaskWorld) => {
      expect(selectDirectory(t.org)(t.store.getState())).toEqual({
        orgName: null,
        workgroups: new Map(),
        members: new Map(),
      });
      putEvents(t.store, t.org, [
        entry(t.org, 1, { kind: 'org.profile' }, { name: 'Old' }),
        entry(t.org, 2, { kind: 'org.profile' }, { name: 'Private' }),
        entry(t.org, 3, { kind: 'org.profile' }, { other: 1 }),
        entry(t.org, 4, { kind: 'wg.profile', wg: 'w1' }, { name: 'Default' }),
        entry(t.org, 5, { kind: 'wg.profile', wg: 'w2' }, null),
        entry(t.org, 6, { kind: 'member.profile', ref: 'u1', author: 'u1' }, { display_name: 'Ada' }),
        entry(t.org, 7, { kind: 'member.profile', ref: 'u1', author: 'u2' }, { display_name: 'Forged' }),
        entry(t.org, 8, { kind: 'member.profile', ref: 'u3', author: 'u3' }, { display_name: 3 }),
        entry(t.org, 9, { kind: 'member.profile', ref: 'u4', author: 'u4' }, { display_name: 'Bad' }, 'unverifiable'),
      ]);
      const directory = selectDirectory(t.org)(t.store.getState());
      expect(directory.orgName).toBe('Private');
      expect([...directory.workgroups]).toEqual([['w1', 'Default']]);
      expect([...directory.members]).toEqual([['u1', 'Ada']]);
      expect(selectDirectory(t.org)(t.store.getState())).toBe(directory);
    });
  });

  it('finds a title by the row title event, else the newest of the task; unverifiable titles carry the code', async () => {
    const t = await taskWorld();
    const select = (task: string, event: string | null) => selectTitle(t.wg, task, event)(t.store.getState());
    expect(select('t1', null)).toEqual({ status: 'missing' });
    putEvents(t.store, t.wg, [
      entry(t.wg, 1, { kind: 'task.meta', task: 't1' }, { title: 'First' }),
      entry(t.wg, 2, { kind: 'task.meta', task: 't1' }, { title: 'Renamed' }),
      entry(t.wg, 3, { kind: 'task.meta', task: 't2' }, { nope: 1 }),
      entry(t.wg, 4, { kind: 'prompt', task: 't3' }, { text: 'x' }),
      entry(t.wg, 5, { id: 'bad' }, null, 'unverifiable'),
      { ...entry(t.wg, 6, {}, null, 'unverifiable'), event: null },
      entry(t.wg, 7, { kind: 'task.meta', task: 't4' }, null, 'pending'),
    ]);
    expect(select('t1', null)).toEqual({ status: 'verified', text: 'Renamed' });
    expect(select('t1', 'e1')).toEqual({ status: 'verified', text: 'First' });
    expect(select('t1', 'unknown')).toEqual({ status: 'verified', text: 'Renamed' });
    // a title event of another task does not name this one (the hub cannot swap titles)
    expect(select('t1', 'e3')).toEqual({ status: 'verified', text: 'Renamed' });
    expect(select('t2', null)).toEqual({ status: 'verified', text: '' });
    expect(select('t3', null)).toEqual({ status: 'missing' });
    expect(select('t5', 'bad')).toEqual({ status: 'unverifiable', code: 'author_not_writer' });
    expect(select('t4', null)).toEqual({ status: 'missing' });
  });

  it('takes the owner from the signed author of the first user-written task.meta, never from the hub', async () => {
    const t = await taskWorld({ member: true });
    const device = await actor('session device');
    await t.world.admitDevice(t.ada, device, [t.wg]);
    setLog(t.store, t.org, t.world.log!);
    const owner = (task: string) => selectOwner(t.org, t.wg, task)(t.store.getState());
    expect(owner('t1')).toBeNull();
    const meta = (seq: number, task: string, author: string, ts: string, status: EventEntry['status'] = 'verified') =>
      entry(t.wg, seq, { kind: 'task.meta', task, author, ts }, { title: task }, status);
    putEvents(t.store, t.wg, [
      // a renamed title of the session's device comes first in scope order but is not the owner
      meta(1, 't1', device.id, '2026-11-21T12:00:01.000Z'),
      meta(2, 't1', t.ada.id, '2026-11-21T12:00:00.000Z'),
      meta(3, 't1', t.me.id, '2026-11-21T12:00:05.000Z'),
      meta(4, 't2', t.ada.id, '2026-11-21T12:00:00.000Z'),
      meta(5, 't2', t.me.id, '2026-11-21T12:00:00.000Z'),
      meta(6, 't3', device.id, '2026-11-21T12:00:00.000Z'),
      meta(7, 't4', t.me.id, '2026-11-21T12:00:00.000Z', 'unverifiable'),
      meta(8, 't5', 'not-a-member', '2026-11-21T12:00:00.000Z'),
      // a writer backdating a task.meta of someone else's task does not become its owner (scope order, not ts)
      meta(9, 't6', t.me.id, '2026-11-21T12:00:00.000Z'),
      meta(10, 't6', t.ada.id, '0001-01-01T00:00:00.000Z'),
      // an author the verified state does not know ends the search
      meta(11, 't7', 'not-a-member', '2026-11-21T12:00:00.000Z'),
      meta(12, 't7', t.me.id, '2026-11-21T12:00:01.000Z'),
    ]);
    expect(owner('t1')).toBe(t.ada.id);
    expect(owner('t2')).toBe(t.ada.id);
    expect(owner('t3')).toBeNull();
    expect(owner('t4')).toBeNull();
    expect(owner('t5')).toBeNull();
    expect(owner('t6')).toBe(t.me.id);
    expect(owner('t7')).toBeNull();
    expect(selectOwner('unknown-org', t.wg, 't1')(t.store.getState())).toBeNull();
  });

  it('pulls the titles of a list once per workgroup, newest pages first, bounded per call and never per row', async () => {
    const t = await taskWorld();
    const rows = [taskRow(t, { task_id: 't1' }), taskRow(t, { task_id: 't2' })] as TaskRow[];
    const targets: string[] = [];
    let seq = 100;
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      targets.push(target);
      seq -= 1;
      return { events: [], next_before: seq, head: 100 };
    });
    t.context.ingest.mockImplementation(async () => {
      if (targets.length === 1)
        putEvents(t.store, t.wg, [entry(t.wg, 1, { kind: 'task.meta', task: 't1' }, { title: 'One' })]);
    });
    await titles(t.context, rows);
    // t2 stays missing: at most TITLE_PAGES pages, all of the workgroup (no `task=`)
    expect(targets).toHaveLength(TITLE_PAGES);
    expect(targets[0]).toBe(eventsTarget(t.wg, { kinds: ['task.meta'] }, { before: NEWEST }, PAGE_MAX));
    expect(targets[1]).toBe(eventsTarget(t.wg, { kinds: ['task.meta'] }, { before: 99 }, PAGE_MAX));
    expect(targets.some((target) => target.includes('task='))).toBe(false);
    // the next call continues where the last one stopped; a title in the store needs no pull
    await titles(t.context, rows);
    expect(targets[TITLE_PAGES]).toBe(eventsTarget(t.wg, { kinds: ['task.meta'] }, { before: 90 }, PAGE_MAX));
    targets.length = 0;
    await titles(t.context, [rows[0]!]);
    expect(targets).toEqual([]);
    // the end of the pages and failures end the reads; a sign-out starts over at the newest page
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      targets.push(target);
      return { events: [], next_before: null, head: 100 };
    });
    await titles(t.context, rows);
    await titles(t.context, rows);
    expect(targets).toHaveLength(1);
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    await titles(t.context, [taskRow(t, { task_id: 't3', wg_id: 'other' }) as TaskRow]);
    targets.length = 0;
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      targets.push(target);
      t.store.reset();
      return { events: [], next_before: 5, head: 100 };
    });
    t.store.reset();
    await titles(t.context, rows);
    expect(targets).toEqual([eventsTarget(t.wg, { kinds: ['task.meta'] }, { before: NEWEST }, PAGE_MAX)]);
  });
});
