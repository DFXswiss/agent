// Slice `whiteboards` (06-web.md section 6.1): per board location the entry rows of `GET /v1/whiteboards` (metadata
// only: IDs, author, times, deleted flag, position of the newest event; every page) and the load state; per entry the
// load state of its history (`GET /v1/whiteboards/{entry}/history`); and the sealed events kept for an unchanged retry
// after a failure that a retry can fix (06-web.md section 12.5: retries re-use the same IDs). The text of an entry is
// never here: it exists only inside the events of the `events` slice, decrypted by the reader.
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
  /** Every row of the board, newest first, as the hub listed them. */
  readonly rows: readonly WhiteboardRow[];
  /** The highest `scope_seq` of the board's scope in the store before the rows were listed (null before a load). */
  readonly checked: number | null;
}

export interface Load {
  readonly status: LoadStatus;
  readonly code: string | null;
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

const EMPTY_BOARD: BoardLoad = { status: 'loading', code: null, rows: [], checked: null };

/** Marks a board loading (rows of earlier pages stay). */
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

/** The rows of a completed load and the scope position they cover (`ready`). */
export function putBoardRows(store: Store, key: string, rows: readonly WhiteboardRow[], checked: number): void {
  store.update('whiteboards', (state) => ({
    ...state,
    boards: { ...state.boards, [key]: { status: 'ready', code: null, rows, checked } },
  }));
}

/** A failed load with the catalog code (rows of an earlier load stay). */
export function setBoardError(store: Store, key: string, code: string): void {
  store.update('whiteboards', (state) => {
    const board = state.boards[key] ?? EMPTY_BOARD;
    return { ...state, boards: { ...state.boards, [key]: { ...board, status: 'error', code } } };
  });
}

export function setHistoryLoad(store: Store, entry: string, status: LoadStatus, code: string | null = null): void {
  store.update('whiteboards', (state) => {
    const current = state.histories[entry];
    if (current !== undefined && current.status === status && current.code === code) return state;
    return { ...state, histories: { ...state.histories, [entry]: { status, code } } };
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
