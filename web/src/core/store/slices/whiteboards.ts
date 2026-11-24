// Slice `whiteboards` (06-web.md section 6.1): per board location the entry rows of `GET /v1/whiteboards` (metadata
// only: IDs, author, times, deleted flag, position of the newest event; the pages read so far, newest first, with the
// cursor of the next older page, ADR-086 section 2) and the load state; per entry the load state of its history
// (`GET /v1/whiteboards/{entry}/history`, with the position of the next older page); and the sealed events kept for an
// unchanged retry after a failure that a retry can fix (06-web.md section 12.5: retries re-use the same IDs). The text
// of an entry is never here: it exists only inside the events of the `events` slice, decrypted by the reader.
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

export type LoadStatus = 'loading' | 'ready' | 'error';

/** One entry as `GET /v1/whiteboards` lists it (03-api.md section 4.9). */
export interface WhiteboardRow {
  readonly entry_id: string;
  readonly org_id: string;
  readonly level: string;
  readonly wg_id: string | null;
  readonly task_id: string | null;
  readonly author_id: string;
  readonly created_at: string;
  readonly updated_at: string;
  readonly deleted: boolean;
  readonly latest_event: { readonly scope_id: string; readonly scope_seq: number };
}

export interface BoardLoad {
  readonly status: LoadStatus;
  readonly code: string | null;
  /** The rows of the pages read so far, newest first, as the hub listed them. */
  readonly rows: readonly WhiteboardRow[];
  /** The highest `scope_seq` of the board's scope in the store before the first page was listed (null before). */
  readonly checked: number | null;
  /** The cursor (`next_before`) of the next older page of rows; null when every row is listed. */
  readonly next: string | null;
}

export interface Load {
  readonly status: LoadStatus;
  readonly code: string | null;
  /** `before` of the next older page of versions; null when the history is read to its start (or cannot be paged). */
  readonly next: number | null;
}

/** A sealed event kept for a retry with identical content. */
export interface KeptWrite {
  /** What the user asked for (kind, entry, text, notify): a different request seals anew. */
  readonly request: string;
  readonly event: unknown;
}

export interface WhiteboardsState {
  readonly boards: Readonly<Record<string, BoardLoad>>;
  readonly histories: Readonly<Record<string, Load>>;
  readonly kept: Readonly<Record<string, KeptWrite>>;
}

declare module '../store' {
  interface StoreState {
    whiteboards: WhiteboardsState;
  }
}

export const slice: Slice<'whiteboards'> = {
  name: 'whiteboards',
  initial: () => ({ boards: {}, histories: {}, kept: {} }),
};

const isId = (value: unknown): value is string => typeof value === 'string' && value !== '';
const isIdOrNull = (value: unknown): boolean => value === null || isId(value);

export function isWhiteboardRow(value: unknown): value is WhiteboardRow {
  return (
    isRecord(value) &&
    isId(value.entry_id) &&
    isId(value.org_id) &&
    typeof value.level === 'string' &&
    isIdOrNull(value.wg_id) &&
    isIdOrNull(value.task_id) &&
    isId(value.author_id) &&
    typeof value.created_at === 'string' &&
    typeof value.updated_at === 'string' &&
    typeof value.deleted === 'boolean' &&
    isRecord(value.latest_event) &&
    isId(value.latest_event.scope_id) &&
    Number.isSafeInteger(value.latest_event.scope_seq)
  );
}

const EMPTY_BOARD: BoardLoad = { status: 'loading', code: null, rows: [], checked: null, next: null };

/** Marks a board loading – its first page or the next older one (rows of earlier pages stay). */
export function setBoardLoading(store: Store, key: string): void {
  store.update('whiteboards', (state) => {
    const board = state.boards[key];
    if (board?.status === 'loading') return state;
    return {
      ...state,
      boards: { ...state.boards, [key]: { ...(board ?? EMPTY_BOARD), status: 'loading', code: null } },
    };
  });
}

/** The first page of rows, the scope position they cover and the cursor of the next page (`ready`). */
export function putBoardRows(
  store: Store,
  key: string,
  rows: readonly WhiteboardRow[],
  checked: number,
  next: string | null = null,
): void {
  store.update('whiteboards', (state) => ({
    ...state,
    boards: { ...state.boards, [key]: { status: 'ready', code: null, rows, checked, next } },
  }));
}

/** An older page of rows appended to the read ones (rows listed already are not repeated) and the next cursor. */
export function appendBoardRows(store: Store, key: string, rows: readonly WhiteboardRow[], next: string | null): void {
  store.update('whiteboards', (state) => {
    const board = state.boards[key] ?? EMPTY_BOARD;
    const known = new Set(board.rows.map((row) => row.entry_id));
    const fresh = rows.filter((row) => !known.has(row.entry_id));
    const merged = { ...board, status: 'ready' as const, code: null, rows: [...board.rows, ...fresh], next };
    return { ...state, boards: { ...state.boards, [key]: merged } };
  });
}

/** A failed load with the catalog code (rows of an earlier load stay). */
export function setBoardError(store: Store, key: string, code: string): void {
  store.update('whiteboards', (state) => {
    const board = state.boards[key] ?? EMPTY_BOARD;
    return { ...state, boards: { ...state.boards, [key]: { ...board, status: 'error', code } } };
  });
}

/** The load state of a history; `next` (the next older page) stays as it was unless given. */
export function setHistoryLoad(
  store: Store,
  entry: string,
  status: LoadStatus,
  code: string | null = null,
  next?: number | null,
): void {
  store.update('whiteboards', (state) => {
    const current = state.histories[entry];
    const after = next === undefined ? (current?.next ?? null) : next;
    if (current !== undefined && current.status === status && current.code === code && current.next === after) {
      return state;
    }
    return { ...state, histories: { ...state.histories, [entry]: { status, code, next: after } } };
  });
}

/** Keeps (or with null forgets) the sealed event of a write. */
export function keepWrite(store: Store, key: string, kept: KeptWrite | null): void {
  store.update('whiteboards', (state) => {
    if (kept === null && state.kept[key] === undefined) return state;
    const next = { ...state.kept };
    if (kept === null) delete next[key];
    else next[key] = kept;
    return { ...state, kept: next };
  });
}
