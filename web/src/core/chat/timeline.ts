// The chat timeline of a task (06-web.md section 5.2): the verified events of the workgroup scope that belong to the
// task, ordered by `(ts, scope_seq)` (chat is ascending, section 12.4), plus system rows from the session rows:
//
// - every verified event of the task except `task.meta` (the title) and `session.hook` other than `deny`; the UI
//   renders the kinds it has a chat-item renderer for (`prompt`, `session.*`, `operation.*`, `workvm.*`,
//   `test.run`); of the `workvm.run` items of one job and the `test.run` items of one run only the latest is kept
//   (the final one replaces the running one, `workvm.ts`);
// - unverifiable events whose (unchecked) header names the task, as neutral rows with the code – never as content,
//   never dropped; pending and locked events appear once the reader settled them;
// - `session.status` system rows for the session transitions `suspended`, `stopped` and `failed` and a row when the
//   task was closed;
// - a `countdown` item per main session at its creation (the live countdown of the current start, the summary of the
//   earlier ones, section 4.4); its body names the task;
// - `session.status` events of the `waiting` state (an operation the platform holds until the task owner acts,
//   KOR-25) folded into one card per kind (`core/github/waiting.ts`); they are no session transition.
//
// Output of a subagent session carries the node of the session tree (indent and label "Subagent 1.2"). The session
// chat (`/o/:org/s/:session`, tab Chat) is the same timeline restricted to the session's own events.
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import type { EventEntry, ScopeEvents } from '../store/slices/events';
import type { SessionEntry, TaskRow, TasksState } from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import { flatten, type SessionNode, sessionTree } from '../sessions/tree';
import { latestRuns } from './workvm';
import { foldWaiting, type WaitingCard } from '../github/waiting';

export const SYSTEM_STATES: ReadonlySet<string> = new Set(['suspended', 'stopped', 'failed']);
export const HIDDEN_KINDS: ReadonlySet<string> = new Set(['task.meta', 'session.terminal']);

/** Rank of items at the same time: events by `scope_seq` first, then system rows. */
const SYSTEM_SEQ = Number.MAX_SAFE_INTEGER;

export type ItemSource = 'event' | 'unverifiable' | 'transition' | 'task' | 'countdown';

export interface TimelineItem {
  readonly key: string;
  /** The chat-item kind: the event kind, `session.status` for system rows, `countdown`, or `unverifiable`. */
  readonly kind: string;
  readonly source: ItemSource;
  /** Unix ms of the item (`header.ts`; device time of a transition; hub time of a session start or a close). */
  readonly at: number;
  readonly seq: number;
  readonly sessionId: string | null;
  readonly author: string | null;
  /** The decrypted body (events) or the system row's data. */
  readonly body: unknown;
  /** The reader's code (`unverifiable`). */
  readonly code: string | null;
  /** The session-tree node of a subagent session's item, else null. */
  readonly subagent: SessionNode | null;
  /** The folded card of a valid `waiting` status event (only the newest item of its kind carries it). */
  readonly waiting?: WaitingCard;
}

const rawHeader = (entry: EventEntry): Readonly<Record<string, unknown>> =>
  isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};

/** A time that parses, else `fallback`. */
function timeOr(value: unknown, fallback: number): number {
  try {
    return parseTime(value);
  } catch {
    return fallback;
  }
}

const isDeny = (body: unknown): boolean => isRecord(body) && body.decision === 'deny';

function eventItem(entry: EventEntry, nodes: ReadonlyMap<string, SessionNode>): TimelineItem | null {
  const header = entry.header!;
  const kind = header.kind as string;
  if (HIDDEN_KINDS.has(kind) || (kind === 'session.hook' && !isDeny(entry.body))) return null;
  const session = typeof header.session === 'string' && header.session !== '' ? header.session : null;
  const node = session === null ? undefined : nodes.get(session);
  return {
    key: `e${entry.scopeSeq}`,
    kind,
    source: 'event',
    at: timeOr(header.ts, 0),
    seq: entry.scopeSeq,
    sessionId: session,
    author: header.author as string,
    body: entry.body,
    code: null,
    subagent: node !== undefined && node.depth > 0 ? node : null,
  };
}

function unverifiableItem(entry: EventEntry): TimelineItem {
  const header = rawHeader(entry);
  return {
    key: `e${entry.scopeSeq}`,
    kind: 'unverifiable',
    source: 'unverifiable',
    at: timeOr(header.ts, timeOr(entry.receivedAt, 0)),
    seq: entry.scopeSeq,
    sessionId: typeof header.session === 'string' && header.session !== '' ? header.session : null,
    author: null,
    body: null,
    code: entry.code,
    subagent: null,
  };
}

/** A settled event of the task; unverifiable ones by their unchecked header, never of a hidden kind. */
const belongs = (entry: EventEntry, task: string): boolean => {
  if (entry.status === 'verified') return entry.header!.task === task;
  const header = rawHeader(entry);
  return header.task === task && !HIDDEN_KINDS.has(header.kind as string);
};

function systemItems(entry: SessionEntry): TimelineItem[] {
  return entry.transitions
    .filter((transition) => SYSTEM_STATES.has(transition.state))
    .map((transition) => ({
      key: `t${entry.row.session_id}:${transition.seq}`,
      kind: 'session.status',
      source: 'transition' as const,
      at: timeOr(transition.at, 0),
      seq: SYSTEM_SEQ,
      sessionId: entry.row.session_id,
      author: null,
      body: { state: transition.state, reason: transition.reason },
      code: null,
      subagent: null,
    }));
}

const compare = (a: TimelineItem, b: TimelineItem): number =>
  a.at - b.at || a.seq - b.seq || Number(a.key > b.key) - Number(a.key < b.key);

export interface TimelineInput {
  readonly scope: ScopeEvents | undefined;
  readonly task: TaskRow;
  readonly sessions: TasksState['sessions'];
  /** Restrict to one session's own `session.*` events (the session chat). */
  readonly session?: string;
}

/** The ordered timeline (module comment). */
export function buildTimeline(input: TimelineInput): TimelineItem[] {
  const { scope, task, sessions } = input;
  const tree = sessionTree(sessions, task.task_id);
  const nodes = new Map(flatten(tree).map((node) => [node.entry.row.session_id, node]));
  const items: TimelineItem[] = [];
  for (const entry of scope?.entries.values() ?? []) {
    if (entry.status === 'pending' || entry.status === 'locked' || !belongs(entry, task.task_id)) continue;
    const item = entry.status === 'verified' ? eventItem(entry, nodes) : unverifiableItem(entry);
    if (item !== null) items.push(item);
  }
  if (input.session !== undefined) {
    const session = input.session;
    return foldWaiting(latestRuns(items.filter((item) => item.sessionId === session).sort(compare)));
  }
  for (const node of flatten(tree)) items.push(...systemItems(node.entry));
  for (const node of tree) {
    items.push({
      key: `c${node.entry.row.session_id}`,
      kind: 'countdown',
      source: 'countdown',
      at: timeOr(node.entry.row.created_at, 0),
      seq: SYSTEM_SEQ,
      sessionId: node.entry.row.session_id,
      author: null,
      body: { task: task.task_id },
      code: null,
      subagent: null,
    });
  }
  if (task.closed_at !== null) {
    items.push({
      key: 'closed',
      kind: 'session.status',
      source: 'task',
      at: timeOr(task.closed_at, 0),
      seq: SYSTEM_SEQ,
      sessionId: null,
      author: null,
      body: { state: 'closed', reason: null },
      code: null,
      subagent: null,
    });
  }
  return foldWaiting(latestRuns(items.sort(compare)));
}

/**
 * A selector of the timeline of `task` (optionally of one session), memoized on the scope's events, the task row and
 * the session rows, so a component re-renders only when one of them changed.
 */
export function selectTimeline(wg: string, task: string, session?: string): (state: StoreState) => TimelineItem[] {
  let last: { scope: unknown; row: unknown; sessions: unknown; items: TimelineItem[] } | null = null;
  const empty: TimelineItem[] = [];
  return (state) => {
    const scope = state.events[wg];
    const row = state.tasks.tasks[task];
    if (row === undefined) return empty;
    const sessions = state.tasks.sessions;
    if (last !== null && last.scope === scope && last.row === row && last.sessions === sessions) return last.items;
    const items = buildTimeline({ scope, task: row, sessions, ...(session !== undefined && { session }) });
    last = { scope, row, sessions, items };
    return items;
  };
}
