// The board projection (06-web.md sections 1 and 6.1): events read by the real event reader, applied with the hub's
// rules (first entry wins, edits and deletes only on live entries), versions, neutral rows for unverifiable events
// (a forged entry of a non-writer is `author_not_writer`), bodies that do not fit, locked events, pending events, and
// placeholders for rows the hub lists without events; newest first.
import { describe, expect, it } from 'vitest';
import { orgEntry } from '../../../src/core/store/slices/orgs';
import type { EventEntry } from '../../../src/core/store/slices/events';
import type { WhiteboardRow } from '../../../src/core/store/slices/whiteboards';
import { orgBoard, taskBoard, wgBoard } from '../../../src/core/whiteboards/location';
import {
  type BoardEntry,
  neutralLabel,
  projectBoard,
  TEXT_MAX,
  validText,
} from '../../../src/core/whiteboards/project';
import { T0 } from '../log/world';
import { boardEvent, boardWorld, deliver, entryId } from './world';

const TASK = '0190f000-0000-7000-8000-00000000000c';
const BASE = '0190f000-0000-7000-8000-0000000000b0';

const row = (entry: string, seq: number, overrides: Partial<WhiteboardRow> = {}): WhiteboardRow => ({
  entry_id: entry,
  org_id: 'o',
  level: 'org',
  wg_id: null,
  task_id: null,
  author_id: 'a',
  created_at: '2026-11-21T12:00:00.000Z',
  updated_at: '2026-11-21T12:00:00.000Z',
  deleted: false,
  latest_event: { scope_id: 'o', scope_seq: seq },
  ...overrides,
});

describe('validText', () => {
  it('accepts 1 to 65 536 code points', () => {
    expect(validText('')).toBe(false);
    expect(validText(42)).toBe(false);
    expect(validText('x'.repeat(TEXT_MAX))).toBe(true);
    expect(validText('x'.repeat(TEXT_MAX + 1))).toBe(false);
    expect(validText('😀'.repeat(TEXT_MAX))).toBe(true);
  });
});

describe('projectBoard', () => {
  it('applies entries, edits and deletes with versions, newest first', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const [a, b] = [await entryId('a'), await entryId('b')];
    await deliver(w, board, [
      await boardEvent(w, board, 'whiteboard.entry', a, { level: 'org', text: 'First' }),
      await boardEvent(w, board, 'whiteboard.entry', b, { level: 'org', text: 'Second' }),
      await boardEvent(w, board, 'whiteboard.edit', a, { text: 'First, edited', base_event_id: BASE }),
      await boardEvent(w, board, 'whiteboard.delete', b, { base_event_id: BASE }),
      // ignored like the hub: a second entry with the same ID, an edit of an unknown entry, an edit after the delete
      await boardEvent(w, board, 'whiteboard.entry', a, { level: 'org', text: 'Again' }),
      await boardEvent(w, board, 'whiteboard.edit', await entryId('unknown'), { text: 'x', base_event_id: BASE }),
      await boardEvent(w, board, 'whiteboard.edit', b, { text: 'late', base_event_id: BASE }),
    ]);
    const view = projectBoard(w.store.getState().events[w.org], board, [], 0);
    expect(view.items.map((item) => item.type)).toEqual(['entry', 'entry']);
    const [second, first] = view.items as [BoardEntry, BoardEntry];
    expect(first).toMatchObject({ id: a, text: 'First, edited', edited: true, deleted: false, author: w.ada.id });
    expect(first.createdAt).toBe(T0);
    expect(first.versions.map((v) => [v.kind, v.text])).toEqual([
      ['whiteboard.entry', 'First'],
      ['whiteboard.edit', 'First, edited'],
    ]);
    expect(first.latestEventId).toBe(first.versions[1]!.eventId);
    expect(second).toMatchObject({ id: b, text: 'Second', edited: false, deleted: true });
    expect(second.versions.map((v) => v.text)).toEqual(['Second', null]);
    expect(view.entries.size).toBe(2);
  });

  it('shows a forged entry of a reader as a neutral row and never its text', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    await deliver(w, board, [
      await boardEvent(w, board, 'whiteboard.entry', await entryId('forged'), { level: 'org', text: 'Forged' }, w.bob),
    ]);
    const view = projectBoard(w.store.getState().events[w.org], board, [], 0);
    expect(view.items).toEqual([
      { type: 'unverifiable', key: 'n1', seq: 1, code: 'author_not_writer', ref: await entryId('forged') },
    ]);
  });

  it('refuses bodies that do not fit the kind or the level', async () => {
    const w = await boardWorld();
    const board = wgBoard(w.org, w.wg);
    const id = await entryId('level');
    await deliver(w, board, [
      await boardEvent(w, board, 'whiteboard.entry', id, { level: 'task', text: 'Wrong level' }),
      await boardEvent(w, board, 'whiteboard.entry', await entryId('n'), 'not an object'),
      await boardEvent(w, board, 'whiteboard.entry', await entryId('ok'), { level: 'workgroup', text: 'Fine' }),
      await boardEvent(w, board, 'whiteboard.edit', await entryId('ok'), { text: '', base_event_id: BASE }),
      await boardEvent(w, board, 'whiteboard.edit', await entryId('ok'), { text: 'y' }),
      await boardEvent(w, board, 'whiteboard.delete', await entryId('ok'), {}),
      await boardEvent(w, board, 'whiteboard.delete', await entryId('ok'), { base_event_id: 'not-a-uuid' }),
      await boardEvent(w, board, 'whiteboard.delete', await entryId('ok'), { base_event_id: BASE, extra: 1 }),
    ]);
    const view = projectBoard(w.store.getState().events[w.wg], board, [], 0);
    expect(view.items.map((item) => [item.type, 'code' in item ? item.code : null])).toEqual([
      ...Array.from({ length: 5 }, () => ['unverifiable', 'header_invalid']),
      ['entry', null],
      ['unverifiable', 'header_invalid'],
      ['unverifiable', 'header_invalid'],
    ]);
  });

  it('keeps boards apart: the workgroup board without task entries, the task board only its task', async () => {
    const w = await boardWorld();
    const wg = wgBoard(w.org, w.wg);
    const task = taskBoard(w.org, w.wg, TASK);
    await deliver(w, wg, [
      await boardEvent(w, wg, 'whiteboard.entry', await entryId('wg'), { level: 'workgroup', text: 'WG' }),
      await boardEvent(w, task, 'whiteboard.entry', await entryId('task'), { level: 'task', text: 'Task' }),
      await boardEvent(w, wg, 'prompt', await entryId('p'), { text: 'not a board event' }),
    ]);
    const scope = w.store.getState().events[w.wg];
    expect((projectBoard(scope, wg, [], 0).items[0] as { text: string }).text).toBe('WG');
    expect(projectBoard(scope, wg, [], 0).items).toHaveLength(1);
    expect((projectBoard(scope, task, [], 0).items[0] as { text: string }).text).toBe('Task');
    expect(projectBoard(undefined, task, [], 0).items).toEqual([]);
  });

  it('shows locked events as neutral rows and skips pending ones', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const locked = await boardEvent(w, board, 'whiteboard.entry', await entryId('l'), { level: 'org', text: 'L' });
    w.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [w.org]: { ...orgEntry(orgs, w.org), keys: new Map() } },
    }));
    await deliver(w, board, [locked]);
    const pending: EventEntry = {
      org: w.org,
      scopeId: w.org,
      scopeSeq: 9,
      receivedAt: null,
      event: { header: { kind: 'whiteboard.entry', org: w.org, scope: 'org', scope_id: w.org, task: '' } },
      status: 'pending',
      code: null,
      header: null,
      body: null,
    };
    const scope = w.store.getState().events[w.org]!;
    const entries = new Map(scope.entries).set(9, pending);
    const view = projectBoard({ entries }, board, [], 0);
    expect(view.items).toEqual([{ type: 'locked', key: 'n1', seq: 1, code: null, ref: await entryId('l') }]);
  });

  it('adds a missing row for a listed entry without events', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const known = await entryId('known');
    await deliver(w, board, [await boardEvent(w, board, 'whiteboard.entry', known, { level: 'org', text: 'K' })]);
    const rows = [row(known, 1), row('gone', 5), row('also', 5)];
    const scope = w.store.getState().events[w.org];
    expect(projectBoard(scope, board, rows, 1).items.map((item) => [item.type, item.key])).toEqual([
      ['missing', 'palso'],
      ['missing', 'pgone'],
      ['entry', `e${known}`],
    ]);
  });

  it('reads unverifiable events by their raw header and ignores raw headers that are not objects', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const raw = (seq: number, event: unknown): EventEntry => ({
      org: w.org,
      scopeId: w.org,
      scopeSeq: seq,
      receivedAt: null,
      event,
      status: 'unverifiable',
      code: 'signature_invalid',
      header: null,
      body: null,
    });
    const header = { kind: 'whiteboard.edit', org: w.org, scope: 'org', scope_id: w.org, task: '', ref: '' };
    const entries = new Map([
      [1, raw(1, { header })],
      [2, raw(2, 'garbage')],
      [3, raw(3, { header: 'garbage' })],
    ]);
    expect(projectBoard({ entries }, board, [], 0).items).toEqual([
      { type: 'unverifiable', key: 'n1', seq: 1, code: 'signature_invalid', ref: null },
    ]);
  });

  it('gives versions without a parseable time the time 0', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    await deliver(w, board, [
      await boardEvent(w, board, 'whiteboard.entry', await entryId('t'), { level: 'org', text: 'T' }),
    ]);
    const scope = w.store.getState().events[w.org]!;
    const [entry] = [...scope.entries.values()];
    const changed = { ...entry!, header: { ...entry!.header!, ts: 'yesterday' } };
    const view = projectBoard({ entries: new Map([[1, changed]]) }, board, [], 0);
    expect((view.items[0] as { createdAt: number }).createdAt).toBe(0);
  });

  it('shows an entry ID claimed on another board of the scope as a conflict, like the hub', async () => {
    const w = await boardWorld();
    const wg = wgBoard(w.org, w.wg);
    const task = taskBoard(w.org, w.wg, TASK);
    const id = await entryId('reused');
    await deliver(w, wg, [
      await boardEvent(w, wg, 'whiteboard.entry', id, { level: 'workgroup', text: 'First' }),
      await boardEvent(w, task, 'whiteboard.entry', id, { level: 'task', text: 'Reused' }),
    ]);
    const scope = w.store.getState().events[w.wg];
    expect((projectBoard(scope, wg, [], null).items[0] as BoardEntry).text).toBe('First');
    expect(projectBoard(scope, task, [], null).items).toEqual([
      { type: 'unverifiable', key: 'n2', seq: 2, code: 'event_conflict', ref: id },
    ]);
  });

  it('shows an entry the hub did not list although it existed at the listing as a conflict', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    const [listed, foreign, later] = [await entryId('listed'), await entryId('foreign'), await entryId('later')];
    await deliver(w, board, [
      await boardEvent(w, board, 'whiteboard.entry', listed, { level: 'org', text: 'Listed' }),
      await boardEvent(w, board, 'whiteboard.entry', foreign, { level: 'org', text: 'Used elsewhere' }),
      await boardEvent(w, board, 'whiteboard.entry', later, { level: 'org', text: 'Live' }),
    ]);
    const scope = w.store.getState().events[w.org];
    const view = projectBoard(scope, board, [row(listed, 1)], 2);
    expect(view.items.map((item) => [item.type, 'ref' in item ? item.ref : item.id])).toEqual([
      ['entry', later],
      ['unverifiable', foreign],
      ['entry', listed],
    ]);
    expect((view.items[1] as { code: string }).code).toBe('event_conflict');
    expect(view.entries.has(foreign)).toBe(false);
  });

  it('ignores unverifiable events of other boards', async () => {
    const w = await boardWorld();
    const raw: EventEntry = {
      org: w.org,
      scopeId: w.wg,
      scopeSeq: 1,
      receivedAt: null,
      event: { header: { kind: 'whiteboard.entry', org: w.org, scope: 'wg', scope_id: w.wg, task: TASK, ref: '' } },
      status: 'unverifiable',
      code: 'signature_invalid',
      header: null,
      body: null,
    };
    expect(projectBoard({ entries: new Map([[1, raw]]) }, wgBoard(w.org, w.wg), [], null).items).toEqual([]);
  });
});

describe('neutralLabel', () => {
  it('names the code of unverifiable rows and none for the others', () => {
    const base = { key: 'k', seq: 1, ref: null };
    expect(neutralLabel({ ...base, type: 'unverifiable', code: 'author_not_writer' })).toEqual({
      key: 'whiteboard.item.unverifiable',
      params: { code: 'author_not_writer' },
      code: 'author_not_writer',
    });
    expect(neutralLabel({ ...base, type: 'unverifiable', code: null }).code).toBe('unverifiable');
    expect(neutralLabel({ ...base, type: 'locked', code: null })).toEqual({
      key: 'whiteboard.item.locked',
      params: {},
      code: undefined,
    });
    expect(neutralLabel({ type: 'missing', key: 'p', seq: 1, ref: 'e' }).key).toBe('whiteboard.item.missing');
  });
});
