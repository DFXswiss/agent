// Slice `tasks` (06-web.md sections 4 and 5.1): task and session rows as the hub reports them (`POST /v1/tasks`,
// `GET /v1/workgroups/{wg}/tasks`, `GET /v1/tasks/{task}`, `GET /v1/sessions/{id}`, the `task` and `session` frames of
// `GET /v1/stream`), the session transitions of the countdown, the task lists per workgroup and filter (newest first,
// `next_before`), the launches this browser started (anchor of the countdown) and the hub clock offset from
// `hello.hub_time`. Rows carry metadata only; titles and prompts live encrypted in the `events` slice.
//
// The slice also exports `frames` (the live-channel frames it applies, `core/net/frames.ts`).
import { parseTime } from '../../encoding/time';
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

/** `task_row` of the contracts (02-data-model.md section 2.4). */
export interface TaskRow {
  readonly task_id: string;
  readonly org_id: string;
  readonly wg_id: string;
  readonly owner_id: string;
  readonly state: string;
  readonly state_reason: string | null;
  readonly created_at: string;
  readonly state_at: string;
  readonly last_activity_at: string;
  readonly closed_at: string | null;
  readonly main_session_id: string | null;
  readonly title_event_id: string | null;
  /** The chosen device and VM (ADR-075 section 6); only present when set. */
  readonly placement?: { readonly device_id: string; readonly vm_id: string | null } | null;
  /** The GitHub identity chosen for the task (ADR-078 section 3); absent from hubs before KOR-47. */
  readonly github_identity?: string | null;
  /** The rights profile of the task's main sessions (ADR-090 section 1); absent from hubs before KOR-51. */
  readonly agent_profile?: unknown;
}

/** The fields of `session_row` the client uses (the row is kept as received). */
export interface SessionRow {
  readonly session_id: string;
  readonly task_id: string;
  readonly org_id: string;
  readonly wg_id: string;
  readonly device_id: string | null;
  /** The VM the session runs in (from `vm_ready` on, ADR-075). */
  readonly vm_id?: string | null;
  readonly state: string;
  readonly state_reason: string | null;
  readonly estimate_ms: number | null;
  readonly created_at: string;
  readonly first_working_at: string | null;
  /** When the session ended (absent from an older hub: the runtime of an ended session is then not shown). */
  readonly ended_at?: string | null;
  /** The person whose instruction created the session (ADR-083 section 2); absent from an older hub. */
  readonly triggered_by?: string | null;
  /** The rights profile a child was started with (ADR-090 section 1; `profiles.sessionRole`). */
  readonly agent_profile?: unknown;
}

/** `session_transition`: `at` is the device clock, `received_at` the hub clock. */
export interface Transition {
  readonly seq: number;
  readonly state: string;
  readonly reason: string | null;
  readonly at: string;
  readonly received_at: string;
}

export interface SessionEntry {
  readonly row: SessionRow;
  /** Ascending by `seq`, one per `seq`. */
  readonly transitions: readonly Transition[];
  /** Browser time at which a session that waited without an estimate got one (the countdown starts there). */
  readonly estimatedAt: number | null;
}

const FINAL = new Set(['stopped', 'failed']);
const PROGRESS = ['requested', 'vm_assigning', 'vm_ready', 'sandbox_ready', 'agent_starting', 'prompt_delivering'];

/**
 * A session row older than the stored one (a `GET` answer overtaken by frames, a replayed frame). Rows carry no
 * version, so: a row whose own transitions end below the newest known one; a non-final row after a final one; a row
 * without `first_working_at` after one with it; a start-up state before the stored start-up state.
 */
export function staleSession(
  known: SessionEntry | undefined,
  row: SessionRow,
  transitions: readonly Transition[],
): boolean {
  if (known === undefined) return false;
  const newest = known.transitions.at(-1)?.seq ?? 0;
  const own = transitions.reduce((max, item) => Math.max(max, item.seq), 0);
  if (transitions.length > 0 && own < newest) return true;
  if (FINAL.has(known.row.state) && !FINAL.has(row.state)) return true;
  if (known.row.first_working_at !== null && row.first_working_at === null) return true;
  const before = PROGRESS.indexOf(known.row.state);
  const after = PROGRESS.indexOf(row.state);
  return after >= 0 && after < before;
}

export type ListStatus = 'idle' | 'loading' | 'ready' | 'error';

/** One task list (`listKey(wg, state)`): task IDs newest first (latest activity) and the cursor of the next page. */
export interface TaskList {
  readonly wg: string;
  readonly state: string | null;
  readonly ids: readonly string[];
  readonly nextBefore: string | null;
  readonly status: ListStatus;
  readonly code: string | null;
}

/**
 * A task this browser just created: the countdown starts at `min(estimate_ms, 10 s)` from the moment the
 * `POST /v1/tasks` answer arrived (`localAt`, browser clock), and shows the device assignment it made first.
 */
export interface Launch {
  readonly sessionId: string;
  readonly localAt: number;
  readonly estimateMs: number | null;
  /** Duration of the automatic device assignment before the task was created (null: none was needed). */
  readonly assignedMs: number | null;
}

export interface TasksState {
  readonly tasks: Readonly<Record<string, TaskRow>>;
  readonly sessions: Readonly<Record<string, SessionEntry>>;
  readonly lists: Readonly<Record<string, TaskList>>;
  readonly launches: Readonly<Record<string, Launch>>;
  /** Hub clock minus browser clock (ms) from the last `hello`; null before the first one. */
  readonly hubOffsetMs: number | null;
}

declare module '../store' {
  interface StoreState {
    tasks: TasksState;
  }
}

export const slice: Slice<'tasks'> = {
  name: 'tasks',
  initial: () => ({ tasks: {}, sessions: {}, lists: {}, launches: {}, hubOffsetMs: null }),
};

// --- validation of hub rows (malformed rows are ignored, never half-applied) ---

const isText = (value: unknown): value is string => typeof value === 'string';
const isTextOrNull = (value: unknown): value is string | null => value === null || typeof value === 'string';
const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

const TASK_TEXT = ['task_id', 'org_id', 'wg_id', 'owner_id', 'state', 'created_at', 'state_at', 'last_activity_at'];
const TASK_NULLABLE = ['state_reason', 'closed_at', 'main_session_id', 'title_event_id'];
const SESSION_TEXT = ['session_id', 'task_id', 'org_id', 'wg_id', 'state', 'created_at'];
const SESSION_NULLABLE = ['device_id', 'state_reason', 'first_working_at'];

export function isTaskRow(value: unknown): value is TaskRow {
  return (
    isRecord(value) &&
    TASK_TEXT.every((field) => isText(value[field])) &&
    TASK_NULLABLE.every((field) => isTextOrNull(value[field]))
  );
}

export function isSessionRow(value: unknown): value is SessionRow {
  return (
    isRecord(value) &&
    SESSION_TEXT.every((field) => isText(value[field])) &&
    SESSION_NULLABLE.every((field) => isTextOrNull(value[field])) &&
    (value.estimate_ms === null || isCount(value.estimate_ms))
  );
}

export function isTransition(value: unknown): value is Transition {
  return (
    isRecord(value) &&
    isCount(value.seq) &&
    isText(value.state) &&
    isTextOrNull(value.reason) &&
    isText(value.at) &&
    isText(value.received_at)
  );
}

// --- lists ---

/** The key of the list of workgroup `wg` with the state filter `state` (null: all states). */
export const listKey = (wg: string, state: string | null): string => `${wg} ${state ?? ''}`;

const emptyList = (wg: string, state: string | null): TaskList => ({
  wg,
  state,
  ids: [],
  nextBefore: null,
  status: 'idle',
  code: null,
});

const idleLists = new Map<string, TaskList>();

/** The list of `wg`/`state` (an idle empty one when not loaded yet, the same object every time). */
export function taskList(state: TasksState, wg: string, filter: string | null): TaskList {
  const key = listKey(wg, filter);
  const found = state.lists[key];
  if (found !== undefined) return found;
  const idle = idleLists.get(key) ?? emptyList(wg, filter);
  idleLists.set(key, idle);
  return idle;
}

/**
 * Newest first: the latest activity (`last_activity_at`) descending, then `task_id` descending – the hub's order of
 * `order=activity` and its tie-breaker (KOR-32; "newest" is the latest change, CONTRIBUTING.md).
 */
export function newestFirst(tasks: Readonly<Record<string, TaskRow>>, ids: Iterable<string>): string[] {
  const time = (id: string) => parseTime(tasks[id]!.last_activity_at);
  return [...new Set(ids)].sort((a, b) => time(b) - time(a) || Number(b > a) - Number(b < a));
}

/** A row older than the stored one (an answer that was overtaken by a frame): by last activity, then state time. */
export function staleTask(known: TaskRow | undefined, row: TaskRow): boolean {
  if (known === undefined) return false;
  const activity = parseTime(row.last_activity_at) - parseTime(known.last_activity_at);
  return activity < 0 || (activity === 0 && parseTime(row.state_at) < parseTime(known.state_at));
}

function withRows(state: TasksState, rows: readonly TaskRow[]): Readonly<Record<string, TaskRow>> {
  const fresh = rows.filter((row) => !staleTask(state.tasks[row.task_id], row));
  if (fresh.length === 0) return state.tasks;
  return { ...state.tasks, ...Object.fromEntries(fresh.map((row) => [row.task_id, row])) };
}

/** Places a changed task row into every loaded list of its workgroup whose filter it matches (and out of the rest). */
function placeInLists(lists: TasksState['lists'], tasks: TasksState['tasks'], row: TaskRow): TasksState['lists'] {
  let next = lists;
  for (const [key, list] of Object.entries(lists)) {
    if (list.wg !== row.wg_id) continue;
    const matches = list.state === null || list.state === row.state;
    const present = list.ids.includes(row.task_id);
    if (matches === present && !matches) continue;
    const ids = matches ? newestFirst(tasks, [...list.ids, row.task_id]) : list.ids.filter((id) => id !== row.task_id);
    next = { ...next, [key]: { ...list, ids } };
  }
  return next;
}

/** Applies task rows (older rows than the stored ones are ignored; lists follow the row's workgroup and state). */
export function putTasks(store: Store, rows: readonly TaskRow[]): void {
  if (rows.length === 0) return;
  store.update('tasks', (state) => {
    const tasks = withRows(state, rows);
    if (tasks === state.tasks) return state;
    let lists = state.lists;
    for (const row of rows) if (tasks[row.task_id] === row) lists = placeInLists(lists, tasks, row);
    return { ...state, tasks, lists };
  });
}

/** A list starts loading (`reset`: the first page again, e.g. after changing the filter). */
export function listLoading(store: Store, wg: string, filter: string | null): void {
  store.update('tasks', (state) => {
    const key = listKey(wg, filter);
    const list = state.lists[key] ?? emptyList(wg, filter);
    return { ...state, lists: { ...state.lists, [key]: { ...list, status: 'loading', code: null } } };
  });
}

/**
 * A loaded page: its rows are stored and appended to the list (`first`: the list starts over with this page;
 * `refresh`: the first page loaded again after a reconnect – it replaces the rows of its time range, and the rows of
 * further pages already loaded, older than the page, stay with their paging position; KOR-11).
 */
export function listPage(
  store: Store,
  page: {
    wg: string;
    state: string | null;
    rows: readonly TaskRow[];
    nextBefore: string | null;
    first: boolean;
    refresh?: boolean;
  },
): void {
  store.update('tasks', (state) => {
    const key = listKey(page.wg, page.state);
    const tasks = withRows(state, page.rows);
    const list = state.lists[key] ?? emptyList(page.wg, page.state);
    const ids = page.rows.map((row) => row.task_id);
    const last = page.rows.at(-1);
    let older: string[] = [];
    if (page.refresh && page.nextBefore !== null && last !== undefined) {
      const ranked = newestFirst(tasks, [...ids, ...list.ids]);
      older = ranked.slice(ranked.indexOf(last.task_id) + 1);
    }
    const merged = newestFirst(tasks, page.first ? [...ids, ...older] : [...list.ids, ...ids]);
    const nextBefore = older.length > 0 ? list.nextBefore : page.nextBefore;
    const updated: TaskList = { ...list, ids: merged, nextBefore, status: 'ready', code: null };
    return { ...state, tasks, lists: { ...state.lists, [key]: updated } };
  });
}

export function listFailed(store: Store, wg: string, filter: string | null, code: string): void {
  store.update('tasks', (state) => {
    const key = listKey(wg, filter);
    const list = state.lists[key] ?? emptyList(wg, filter);
    return { ...state, lists: { ...state.lists, [key]: { ...list, status: 'error', code } } };
  });
}

// --- sessions ---

/**
 * Applies a session row and transitions (duplicates by `seq` are ignored, the order is by `seq`). `now` is the browser
 * time: a known session without an estimate that now has one records it as `estimatedAt`.
 */
export function putSession(store: Store, row: SessionRow, transitions: readonly Transition[], now: number): void {
  store.update('tasks', (state) => {
    const known = state.sessions[row.session_id];
    const bySeq = new Map((known?.transitions ?? []).map((item) => [item.seq, item]));
    for (const item of transitions) if (!bySeq.has(item.seq)) bySeq.set(item.seq, item);
    const sorted = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
    const current = staleSession(known, row, transitions) ? known!.row : row;
    const estimated = known !== undefined && known.row.estimate_ms === null && current.estimate_ms !== null;
    const estimatedAt = known?.estimatedAt ?? (estimated ? now : null);
    const entry: SessionEntry = { row: current, transitions: sorted, estimatedAt };
    return { ...state, sessions: { ...state.sessions, [row.session_id]: entry } };
  });
}

/** Records the launch of a task created by this browser (the countdown's anchor). */
export function putLaunch(store: Store, taskId: string, launch: Launch): void {
  store.update('tasks', (state) => ({ ...state, launches: { ...state.launches, [taskId]: launch } }));
}

// --- live-channel frames ---

export function setHubOffset(store: Store, offsetMs: number): void {
  store.update('tasks', (state) => (state.hubOffsetMs === offsetMs ? state : { ...state, hubOffsetMs: offsetMs }));
}

/** The frames this slice applies (`core/net/frames.ts`): `hello` (clock offset), `task`, `session`. */
export const frames: Readonly<Record<string, (store: Store, data: unknown, now: number) => void>> = {
  hello: (store, data, now) => {
    if (!isRecord(data) || !isText(data.hub_time)) return;
    const hubTime = Date.parse(data.hub_time);
    if (Number.isFinite(hubTime)) setHubOffset(store, hubTime - now);
  },
  task: (store, data) => {
    if (isTaskRow(data)) putTasks(store, [data]);
  },
  session: (store, data, now) => {
    if (!isRecord(data) || !isSessionRow(data.session)) return;
    putSession(store, data.session, isTransition(data.transition) ? [data.transition] : [], now);
  },
};
