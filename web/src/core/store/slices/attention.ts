// Slice `attention` (06-web.md section 4.0, 03-api.md sections 4.6 and 4.6.0, ADR-064): the attention records of the
// situation view as the hub derives them (one per task: category, reason, the object the next action refers to, since
// when, the occasion counter and the subagent counters), the four groups of an org as windowed lists paged by the hub
// (`GET /v1/orgs/{org}/attention?category=…`), and the counters (`GET /v1/orgs/{org}/attention/summary`).
//
// - Order of a group: `needs_decision` longest waiting first (`since` ascending, then `task_id` ascending), the other
//   groups newest first (`since` descending, then `task_id` descending) – the hub's order and tie-breaker.
// - A record that changes (a page, a live `attention` frame) leaves every group of another category and enters the
//   loaded group of its category when it sorts before that group's last loaded record (or the group has no further
//   page); otherwise the next page brings it, so paging never shows a record twice or in the wrong place.
// - A record older than the stored one (by `updated_at`, then `seq`) is ignored: an answer overtaken by a frame.
// - Live frames are applied in batches: at most one store update per `FRAME_MS` (HOT-05); each batch counts
//   `changes`, which the counters follow (refetched at most once per second, `core/attention/load.ts`).
//
// The slice also exports `frames` (the live-channel frames it applies, `core/net/frames.ts`).
import { parseTime } from '../../encoding/time';
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

/** The categories in the order of the view (`contracts/enums/attention.json`). */
export const CATEGORIES = ['needs_decision', 'blocked', 'working', 'done'] as const;
export type Category = (typeof CATEGORIES)[number];

/** `attention_row` of the contracts (03-api.md section 4.6.0). */
export interface AttentionRow {
  readonly task_id: string;
  readonly category: string;
  readonly reason: string;
  readonly ref_kind: 'operation' | 'session' | null;
  readonly ref_id: string | null;
  readonly since: string;
  readonly seq: number;
  readonly subagents_total: number;
  readonly subagents_active: number;
  readonly updated_at: string;
}

/** The counters of `GET /v1/orgs/{org}/attention/summary`. */
export interface AttentionSummary {
  readonly needs_decision: number;
  readonly needs_decision_mine: number;
  readonly blocked: number;
  readonly working: number;
  readonly done_since: number;
}

export type GroupStatus = 'idle' | 'loading' | 'ready' | 'error';

/** One group of an org: task IDs in the group's order and the cursor of the next page. */
export interface AttentionGroup {
  readonly org: string;
  readonly category: Category;
  readonly ids: readonly string[];
  readonly nextBefore: string | null;
  readonly status: GroupStatus;
  readonly code: string | null;
}

export interface AttentionState {
  /** Task ID → its newest record. */
  readonly rows: Readonly<Record<string, AttentionRow>>;
  /** Task ID → the org it belongs to (from the page that brought it, else the active org of its frame). */
  readonly orgs: Readonly<Record<string, string>>;
  /** `groupKey(org, category)` → the group. */
  readonly groups: Readonly<Record<string, AttentionGroup>>;
  /** Org → its counters (null until the first answer). */
  readonly summaries: Readonly<Record<string, AttentionSummary>>;
  /** Counts the applied batches of live frames (the counters follow it). */
  readonly changes: number;
}

declare module '../store' {
  interface StoreState {
    attention: AttentionState;
  }
}

export const slice: Slice<'attention'> = {
  name: 'attention',
  initial: () => ({ rows: {}, orgs: {}, groups: {}, summaries: {}, changes: 0 }),
};

// --- validation (malformed rows are ignored, never half-applied) ---

const isText = (value: unknown): value is string => typeof value === 'string' && value !== '';
const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const isTime = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  try {
    parseTime(value);
    return true;
  } catch {
    return false;
  }
};

export const isCategory = (value: unknown): value is Category => CATEGORIES.includes(value as Category);

export function isAttentionRow(value: unknown): value is AttentionRow {
  return (
    isRecord(value) &&
    isText(value.task_id) &&
    isCategory(value.category) &&
    isText(value.reason) &&
    (value.ref_kind === null || value.ref_kind === 'operation' || value.ref_kind === 'session') &&
    (value.ref_id === null || isText(value.ref_id)) &&
    isTime(value.since) &&
    isCount(value.seq) &&
    isCount(value.subagents_total) &&
    isCount(value.subagents_active) &&
    isTime(value.updated_at)
  );
}

const SUMMARY_FIELDS = ['needs_decision', 'needs_decision_mine', 'blocked', 'working', 'done_since'] as const;

export function isSummary(value: unknown): value is AttentionSummary {
  return isRecord(value) && SUMMARY_FIELDS.every((field) => isCount(value[field]));
}

// --- order ---

export const groupKey = (org: string, category: Category): string => `${org} ${category}`;

/** Whether record `a` comes before record `b` in the group of `category` (module comment). */
export function before(category: Category, a: AttentionRow, b: AttentionRow): boolean {
  const delta = parseTime(a.since) - parseTime(b.since);
  const ascending = delta < 0 || (delta === 0 && a.task_id < b.task_id);
  return category === 'needs_decision' ? ascending : !ascending;
}

function sorted(category: Category, rows: AttentionState['rows'], ids: Iterable<string>): string[] {
  return [...new Set(ids)].sort((a, b) => (before(category, rows[a]!, rows[b]!) ? -1 : 1));
}

/** A record older than the stored one (by `updated_at`, then `seq`). */
export function staleRow(known: AttentionRow | undefined, row: AttentionRow): boolean {
  if (known === undefined) return false;
  const delta = parseTime(row.updated_at) - parseTime(known.updated_at);
  return delta < 0 || (delta === 0 && row.seq < known.seq);
}

const emptyGroup = (org: string, category: Category): AttentionGroup => ({
  org,
  category,
  ids: [],
  nextBefore: null,
  status: 'idle',
  code: null,
});

const idleGroups = new Map<string, AttentionGroup>();

/** The group of `org`/`category` (an idle empty one when not loaded yet, the same object every time). */
export function attentionGroup(state: AttentionState, org: string, category: Category): AttentionGroup {
  const key = groupKey(org, category);
  const found = state.groups[key];
  if (found !== undefined) return found;
  const idle = idleGroups.get(key) ?? emptyGroup(org, category);
  idleGroups.set(key, idle);
  return idle;
}

/** Places changed records into the loaded groups of their org (module comment). */
function place(state: AttentionState, changed: readonly AttentionRow[]): AttentionState['groups'] {
  let groups = state.groups;
  for (const row of changed) {
    const org = state.orgs[row.task_id]!;
    for (const [key, group] of Object.entries(groups)) {
      if (group.org !== org) continue;
      const present = group.ids.includes(row.task_id);
      const own = group.category === row.category;
      if (!own) {
        if (present) groups = { ...groups, [key]: { ...group, ids: group.ids.filter((id) => id !== row.task_id) } };
        continue;
      }
      const lastId = group.ids.at(-1);
      const reachable =
        group.nextBefore === null || (lastId !== undefined && before(group.category, row, state.rows[lastId]!));
      if (present || reachable) {
        const ids = sorted(group.category, state.rows, [...group.ids, row.task_id]);
        groups = { ...groups, [key]: { ...group, ids } };
      }
    }
  }
  return groups;
}

/**
 * Applies records of `org` (null for live frames: each record's known org, else `fallbackOrg`, the org of the live
 * channel); older ones are ignored, and so are frames when no org is known.
 */
function applyRows(
  state: AttentionState,
  rows: readonly AttentionRow[],
  org: string | null,
  fallbackOrg: string | null,
) {
  const fresh: AttentionRow[] = [];
  let records = state.rows;
  let orgs = state.orgs;
  for (const row of rows) {
    if (staleRow(records[row.task_id], row)) continue;
    const owner = org ?? orgs[row.task_id] ?? fallbackOrg;
    if (owner === null) continue;
    records = { ...records, [row.task_id]: row };
    if (orgs[row.task_id] !== owner) orgs = { ...orgs, [row.task_id]: owner };
    fresh.push(row);
  }
  if (fresh.length === 0) return state;
  const next = { ...state, rows: records, orgs };
  return { ...next, groups: place(next, fresh) };
}

/** A group starts loading. */
export function groupLoading(store: Store, org: string, category: Category): void {
  store.update('attention', (state) => {
    const key = groupKey(org, category);
    const group = state.groups[key] ?? emptyGroup(org, category);
    return { ...state, groups: { ...state.groups, [key]: { ...group, status: 'loading', code: null } } };
  });
}

/**
 * A loaded page of a group: its records are stored and the group continues with them (`first`: the group starts over
 * with this page; `refresh`: the first page again after a reconnect – records of further pages already loaded that
 * sort after the page stay with their paging position, KOR-11).
 */
export function groupPage(
  store: Store,
  page: {
    org: string;
    category: Category;
    rows: readonly AttentionRow[];
    nextBefore: string | null;
    first: boolean;
    refresh?: boolean;
  },
): void {
  store.update('attention', (state) => {
    const key = groupKey(page.org, page.category);
    const own = page.rows.filter((row) => row.category === page.category);
    // the page's records are stored and leave the other groups of the org they were in
    const applied = applyRows(state, own, page.org, null);
    const rows = applied.rows;
    const group = state.groups[key] ?? emptyGroup(page.org, page.category);
    // a record a newer frame moved to another category meanwhile stays out
    const ids = own.map((row) => row.task_id).filter((id) => rows[id]!.category === page.category);
    const last = own.at(-1);
    let older: string[] = [];
    if (page.refresh && page.nextBefore !== null && last !== undefined) {
      const bound = rows[last.task_id]!;
      older = group.ids.filter((id) => !ids.includes(id) && before(page.category, bound, rows[id]!));
    }
    const kept = page.first ? [...ids, ...older] : [...group.ids, ...ids];
    const merged = sorted(page.category, rows, kept);
    const nextBefore = older.length > 0 ? group.nextBefore : page.nextBefore;
    const updated: AttentionGroup = { ...group, ids: merged, nextBefore, status: 'ready', code: null };
    return { ...applied, groups: { ...applied.groups, [key]: updated } };
  });
}

export function groupFailed(store: Store, org: string, category: Category, code: string): void {
  store.update('attention', (state) => {
    const key = groupKey(org, category);
    const group = state.groups[key] ?? emptyGroup(org, category);
    return { ...state, groups: { ...state.groups, [key]: { ...group, status: 'error', code } } };
  });
}

export function putSummary(store: Store, org: string, summary: AttentionSummary): void {
  store.update('attention', (state) => ({ ...state, summaries: { ...state.summaries, [org]: summary } }));
}

// --- live frames, batched ---

/** At most one store update per frame (16 ms, HOT-05). */
export const FRAME_MS = 16;

interface Pending {
  readonly generation: number;
  readonly rows: AttentionRow[];
}

const pending = new WeakMap<Store, Pending>();

/** Writes the buffered frames of `store` now (the frame timer calls it; tests may too). */
export function flushAttention(store: Store): void {
  const buffered = pending.get(store);
  if (buffered === undefined) return;
  pending.delete(store);
  // frames buffered before a sign-out never reach the next user's store
  if (buffered.generation !== store.generation) return;
  const active = store.getState().session.activeOrg;
  store.update('attention', (state) => {
    const next = applyRows(state, buffered.rows, null, active);
    return { ...next, changes: state.changes + 1 };
  });
}

/** Buffers a frame's record; the first one of a batch starts the frame timer. */
export function queueAttention(store: Store, row: AttentionRow): void {
  const known = pending.get(store);
  if (known !== undefined && known.generation === store.generation) {
    known.rows.push(row);
    return;
  }
  pending.set(store, { generation: store.generation, rows: [row] });
  setTimeout(() => flushAttention(store), FRAME_MS);
}

/** The frames this slice applies (`core/net/frames.ts`): `attention` (an `attention_row`). */
export const frames: Readonly<Record<string, (store: Store, data: unknown, now: number) => void>> = {
  attention: (store, data) => {
    if (isAttentionRow(data)) queueAttention(store, data);
  },
};
