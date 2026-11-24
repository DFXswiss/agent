// Newest-first paging of a view's event list (ADR-086 section 1): the newest page once, again after skipped bulk
// events (dropping what lies before the hole) or when forced, older pages on demand, failures and sign-outs.
import { describe, expect, it } from 'vitest';
import { NEWEST } from '../../../src/core/net/pages';
import { HubError } from '../../../src/core/net/signedFetch';
import { noteSkipped, show } from '../../../src/core/store/focus';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { NO_PAGE, setPage } from '../../../src/core/store/slices/pages';
import { loadEarlier, openNewest, type PagedList, pagesKey, selectPages } from '../../../src/core/tasks/pages';
import { taskWorld } from './world';

const stored = (seq: number) => ({ scope_seq: seq, received_at: null, event: { header: {} } });

function entry(scope: string, seq: number, kind: string, task: string): EventEntry {
  return {
    org: 'o',
    scopeId: scope,
    scopeSeq: seq,
    receivedAt: null,
    event: { header: { kind, task, session: 's', ref: 'r' } },
    status: 'pending',
    code: null,
    header: null,
    body: null,
  };
}

describe('paged lists', () => {
  it('names a list by scope and query and starts without a page', () => {
    const list: PagedList = { org: 'o', scope: 'wg', query: { kinds: ['a', 'b'], task: 't', session: 's', ref: 'r' } };
    expect(pagesKey(list)).toBe('wg a,b t s r');
    expect(pagesKey({ org: 'o', scope: 'wg', query: {} })).toBe('wg    ');
    const t = { getState: () => ({ pages: {} }) };
    expect(selectPages(list)(t.getState() as never)).toBe(NO_PAGE);
  });

  it('reads the newest page once, again after a skip (dropping bulk events before the hole), and older pages', async () => {
    const t = await taskWorld();
    const list: PagedList = { org: t.org, scope: t.wg, query: { kinds: ['session.output', 'prompt'], task: 't1' } };
    const targets: string[] = [];
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      targets.push(target);
      return target.includes(`before=${NEWEST}`)
        ? { events: [stored(30), stored(20)], next_before: 20, head: 30 }
        : { events: [stored(10)], next_before: null, head: 30 };
    });
    await Promise.all([openNewest(t.context, list), openNewest(t.context, list)]);
    expect(targets).toHaveLength(1);
    expect(t.ingested[0]!.items.map((item) => item.scope_seq)).toEqual([20, 30]);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ status: 'ready', before: 20, earlier: false });
    await openNewest(t.context, list);
    expect(targets).toHaveLength(1);

    await loadEarlier(t.context, list);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ before: null, earlier: false });
    await loadEarlier(t.context, list);
    expect(targets).toHaveLength(2);

    putEvents(t.store, t.wg, [
      entry(t.wg, 5, 'session.output', 't1'),
      entry(t.wg, 6, 'prompt', 't1'),
      entry(t.wg, 7, 'session.output', 't2'),
      entry(t.wg, 8, 'session.tool', 't1'),
      entry(t.wg, 40, 'session.output', 't1'),
    ]);
    noteSkipped(t.store, 't1', null);
    await openNewest(t.context, list);
    expect(targets).toHaveLength(3);
    // the bulk events of the list below the new page are dropped; others stay
    expect([...t.store.getState().events[t.wg]!.entries.keys()].sort((a, b) => a - b)).toEqual([6, 7, 8, 40]);
    await openNewest(t.context, list, true);
    expect(targets).toHaveLength(4);
  });

  it('matches session and operation filters when dropping, and keeps everything when the page is complete', async () => {
    const t = await taskWorld();
    const list: PagedList = { org: t.org, scope: t.wg, query: { session: 's', ref: 'r' } };
    let next: number | null = 20;
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [stored(20)], next_before: next, head: 20 }));
    await openNewest(t.context, list);
    putEvents(t.store, t.wg, [
      entry(t.wg, 5, 'session.output', 't1'),
      { ...entry(t.wg, 6, 'session.output', 't1'), header: { kind: 'session.output' }, event: 'raw' },
    ]);
    next = null;
    await openNewest(t.context, list, true);
    expect(t.store.getState().events[t.wg]!.entries.has(5)).toBe(true);
    next = 20;
    await openNewest(t.context, list, true);
    expect(t.store.getState().events[t.wg]!.entries.has(5)).toBe(false);
    // a bulk entry without a raw header names no session: it stays
    expect(t.store.getState().events[t.wg]!.entries.has(6)).toBe(true);
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_before: 9, head: 20 }));
    putEvents(t.store, t.wg, [entry(t.wg, 5, 'session.output', 't1')]);
    await openNewest(t.context, list, true);
    expect(t.store.getState().events[t.wg]!.entries.has(5)).toBe(true);
  });

  it('records failures (a failed newest page is read again) and ignores answers after a sign-out', async () => {
    const t = await taskWorld();
    const list: PagedList = { org: t.org, scope: t.wg, query: { task: 't1' } };
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      throw new HubError('rate_limited', 429);
    });
    await openNewest(t.context, list);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ status: 'error', code: 'rate_limited' });
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      throw new Error('network');
    });
    await openNewest(t.context, list);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ status: 'error', code: 'unavailable' });

    setPage(t.store, pagesKey(list), { status: 'ready', before: 9, code: null });
    await loadEarlier(t.context, list);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ earlier: false, code: 'unavailable', before: 9 });
    setPage(t.store, pagesKey(list), { status: 'error' });
    await loadEarlier(t.context, list);

    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      t.store.reset();
      return { events: [stored(1)], next_before: null, head: 1 };
    });
    await openNewest(t.context, list);
    expect(t.store.getState().pages).toEqual({});
    setPage(t.store, pagesKey(list), { status: 'ready', before: 9 });
    await loadEarlier(t.context, list);
    expect(t.store.getState().pages).toEqual({});
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      t.store.reset();
      throw new HubError('unavailable', 503);
    });
    await openNewest(t.context, list);
    setPage(t.store, pagesKey(list), { status: 'ready', before: 9 });
    await loadEarlier(t.context, list);
    expect(t.store.getState().pages).toEqual({});
    expect(t.ingested).toHaveLength(0);
  });

  it('sets the boundary to the new page, drops bulk events below it and raises overlapping lists', async () => {
    const t = await taskWorld();
    const task: PagedList = { org: t.org, scope: t.wg, query: { task: 't1' } };
    const session: PagedList = { org: t.org, scope: t.wg, query: { task: 't1', session: 's' } };
    setPage(t.store, pagesKey(session), { status: 'ready', before: 4, list: { scope: t.wg, query: session.query } });
    putEvents(t.store, t.wg, [entry(t.wg, 5, 'session.output', 't1'), entry(t.wg, 6, 'prompt', 't1')]);
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [stored(20)], next_before: 20, head: 20 }));
    // the first read of the task list already drops what lies below its page
    await openNewest(t.context, task);
    expect(selectPages(task)(t.store.getState())).toMatchObject({
      before: 20,
      list: { scope: t.wg, query: task.query },
    });
    expect([...t.store.getState().events[t.wg]!.entries.keys()]).toEqual([6]);
    // the session list had event 5 above its boundary: it reads it again with "Earlier"
    expect(selectPages(session)(t.store.getState()).before).toBe(6);
    // the list keeps its stored scope and query on a re-read
    const known = selectPages(task)(t.store.getState()).list;
    await openNewest(t.context, task, true);
    expect(selectPages(task)(t.store.getState()).list).toBe(known);
  });

  it('a forced re-read keeps what another mounted list paged back to (review KOR-35)', async () => {
    const t = await taskWorld();
    const task: PagedList = { org: t.org, scope: t.wg, query: { task: 't1' } };
    const session: PagedList = { org: t.org, scope: t.wg, query: { task: 't1', session: 's' } };
    setPage(t.store, pagesKey(task), { status: 'ready', before: 4, list: { scope: t.wg, query: task.query } });
    show(t.store, { scope: t.wg, task: 't1', key: pagesKey(task) });
    putEvents(t.store, t.wg, [entry(t.wg, 5, 'session.output', 't1'), entry(t.wg, 3, 'session.output', 't1')]);
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [stored(20)], next_before: 20, head: 20 }));
    await openNewest(t.context, session, true);
    // event 5 lies at or above the task chat's boundary: it stays, its boundary too; event 3 below it goes
    expect([...t.store.getState().events[t.wg]!.entries.keys()].sort((a, b) => a - b)).toEqual([5]);
    expect(selectPages(task)(t.store.getState()).before).toBe(4);
  });

  it('a forced read while one runs queues one more read after it (forced callers meanwhile share it)', async () => {
    const t = await taskWorld();
    const list: PagedList = { org: t.org, scope: t.wg, query: { task: 't1' } };
    const answers: (() => void)[] = [];
    let head = 10;
    t.client.on(`GET /v1/scopes/${t.wg}/events`, async () => {
      const seq = head;
      await new Promise<void>((resolve) => answers.push(resolve));
      return { events: [stored(seq)], next_before: null, head: seq };
    });
    const first = openNewest(t.context, list);
    const joined = openNewest(t.context, list);
    await Promise.resolve();
    head = 11;
    const forced = openNewest(t.context, list, true);
    const forcedToo = openNewest(t.context, list, true);
    expect(forcedToo).toBe(forced);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(answers).toHaveLength(1);
    answers[0]!();
    await Promise.all([first, joined]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(answers).toHaveLength(2);
    answers[1]!();
    await forced;
    expect(t.ingested.map((read) => read.items[0]!.scope_seq)).toEqual([10, 11]);
    // without a running read a forced call reads at once
    const alone = openNewest(t.context, list, true);
    await new Promise((resolve) => setTimeout(resolve, 0));
    answers[2]!();
    await alone;
    expect(t.ingested).toHaveLength(3);
  });

  it('an older page lowers the boundary unless an eviction raised it meanwhile', async () => {
    const t = await taskWorld();
    const list: PagedList = { org: t.org, scope: t.wg, query: { task: 't1' } };
    setPage(t.store, pagesKey(list), { status: 'ready', before: 50, list: { scope: t.wg, query: list.query } });
    let raise = false;
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      if (raise) setPage(t.store, pagesKey(list), { before: 60 });
      return { events: [stored(40)], next_before: 40, head: 70 };
    });
    await loadEarlier(t.context, list);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ before: 40, earlier: false });
    raise = true;
    await loadEarlier(t.context, list);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ before: 60, earlier: false });
    // a new newest page moves the boundary to it
    raise = false;
    await openNewest(t.context, list, true);
    expect(selectPages(list)(t.store.getState())).toMatchObject({ before: 40 });
  });

  it('changes the paging state only when it differs', async () => {
    const t = await taskWorld();
    setPage(t.store, 'k', { status: 'ready' });
    const pages = t.store.getState().pages;
    setPage(t.store, 'k', { status: 'ready' });
    expect(t.store.getState().pages).toBe(pages);
  });
});
