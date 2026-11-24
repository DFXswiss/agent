// The whiteboards slice on its own: the row check, loads without an earlier state, appended pages without duplicates.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import {
  appendBoardRows,
  isWhiteboardRow,
  putBoardRows,
  setBoardError,
  type WhiteboardRow,
} from '../../../src/core/store/slices/whiteboards';

const row = (entry: string, overrides: Partial<WhiteboardRow> = {}): WhiteboardRow => ({
  entry_id: entry,
  org_id: 'o',
  level: 'task',
  wg_id: 'w',
  task_id: 't',
  author_id: 'a',
  created_at: '2026-11-21T12:00:00.000Z',
  updated_at: '2026-11-21T12:00:00.000Z',
  deleted: true,
  latest_event: { scope_id: 'w', scope_seq: 3 },
  ...overrides,
});

describe('whiteboards slice', () => {
  it('checks rows', () => {
    expect(isWhiteboardRow(row('e'))).toBe(true);
    expect(isWhiteboardRow(row('e', { wg_id: null, task_id: null }))).toBe(true);
    expect(isWhiteboardRow(row('e', { wg_id: '' }))).toBe(false);
    expect(isWhiteboardRow(row('e', { deleted: 'no' as never }))).toBe(false);
    expect(isWhiteboardRow(row('e', { latest_event: { scope_id: 'w', scope_seq: 1.5 } }))).toBe(false);
    expect(isWhiteboardRow(row('e', { latest_event: null as never }))).toBe(false);
    expect(isWhiteboardRow(null)).toBe(false);
  });

  it('records a failed load without an earlier state and keeps the rows of an earlier one', () => {
    const store = createStore();
    setBoardError(store, 'k', 'unavailable');
    expect(store.getState().whiteboards.boards.k).toEqual({
      status: 'error',
      code: 'unavailable',
      rows: [],
      checked: null,
      next: null,
    });
    putBoardRows(store, 'k', [row('a')], 7);
    setBoardError(store, 'k', 'rate_limited');
    expect(store.getState().whiteboards.boards.k).toEqual({
      status: 'error',
      code: 'rate_limited',
      rows: [row('a')],
      checked: 7,
      next: null,
    });
  });

  it('appends older pages without repeating a listed row, also to an unknown board', () => {
    const store = createStore();
    appendBoardRows(store, 'n', [row('a')], 'c');
    expect(store.getState().whiteboards.boards.n).toEqual({
      status: 'ready',
      code: null,
      rows: [row('a')],
      checked: null,
      next: 'c',
    });
    putBoardRows(store, 'k', [row('a')], 3, 'c1');
    appendBoardRows(store, 'k', [row('a'), row('b')], null);
    expect(store.getState().whiteboards.boards.k!.rows.map((item) => item.entry_id)).toEqual(['a', 'b']);
    expect(store.getState().whiteboards.boards.k!.next).toBeNull();
  });
});
