// The chat timeline of a task (06-web.md section 5.2): the verified events of the workgroup scope that belong to the
// task, ordered by `(ts, scope_seq)` (chat is ascending, section 12.4), plus system rows from the session rows:
//
// - every verified event of the task except `task.meta` (the title), `task.agent_name` (the agent's name, shown in the
//   header), the watched kinds `session.terminal` and `browser.action`, and `session.hook` other than `deny`; the UI
//   renders the kinds it has a chat-item renderer for (`prompt`, `session.*`, `operation.*`, `workvm.*`, `test.run`,
//   `browser.state`); of the `workvm.run` items of one job, the `test.run` items of one run and the `browser.state`
//   items of one browser session only the latest is kept
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
import { boundaryOf, type ListQuery } from '../store/slices/pages';
import type { SessionEntry, TaskRow, TasksState } from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import { flatten, type SessionNode, sessionTree } from '../sessions/tree';
import { pagesKey } from '../tasks/pages';
import { latestRuns } from './workvm';
import { foldWaiting, type WaitingCard } from '../github/waiting';

/** The kinds of the chat's pull (everything a chat item may show; `session.terminal` is never pulled here). */
export const CHAT_KINDS = [
  'prompt',
  'session.output',
  'session.tool',
  'session.file',
  'session.hook',
  'session.status',
  'session.error',
  'operation.params',
  'operation.result',
  'workvm.run',
  'workvm.state',
  'test.run',
  'browser.state',
] as const;

/** The query of the paged chat events of a task (with `session`: of one session, the session chat). */
export const chatQuery = (task: string, session?: string): ListQuery => ({
  kinds: CHAT_KINDS,
  task,
  ...(session !== undefined && { session }),
});

export const SYSTEM_STATES: ReadonlySet<string> = new Set(['suspended', 'stopped', 'failed']);
export const HIDDEN_KINDS: ReadonlySet<string> = new Set([
  'task.meta',
  'task.agent_name',
  'session.terminal',
  'browser.action',
]);

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
  /** The event ID of a verified event (the queue's withdrawals name prompts by it). */
  readonly eventId?: string;
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
    eventId: header.id as string,
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
  /** The boundary of the chat's paged list: events below it are not shown (`core/store/slices/pages.ts`). */
  readonly floor?: number;
}

interface Parts {
  readonly nodes: ReadonlyMap<string, SessionNode>;
  /** The system rows (transitions, countdowns, the close), sorted; none for a session chat. */
  readonly system: TimelineItem[];
}

function partsOf(input: TimelineInput): Parts {
  const { task, sessions } = input;
  const tree = sessionTree(sessions, task.task_id);
  const nodes = new Map(flatten(tree).map((node) => [node.entry.row.session_id, node]));
  if (input.session !== undefined) return { nodes, system: [] };
  const system: TimelineItem[] = [];
  for (const node of flatten(tree)) system.push(...systemItems(node.entry));
  for (const node of tree) {
    system.push({
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
    system.push({
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
  return { nodes, system: system.sort(compare) };
}

/** The timeline item of one stored entry for the task (null if it shows none). */
function itemOf(entry: EventEntry, input: TimelineInput, nodes: ReadonlyMap<string, SessionNode>): TimelineItem | null {
  if (entry.status === 'pending' || entry.status === 'locked' || entry.scopeSeq < (input.floor ?? 0)) return null;
  if (!belongs(entry, input.task.task_id)) return null;
  const item = entry.status === 'verified' ? eventItem(entry, nodes) : unverifiableItem(entry);
  if (item === null || (input.session !== undefined && item.sessionId !== input.session)) return null;
  return item;
}

/** Merges two sorted lists (linear). */
function merge(a: readonly TimelineItem[], b: readonly TimelineItem[]): TimelineItem[] {
  if (b.length === 0) return [...a];
  if (a.length === 0) return [...b];
  const out: TimelineItem[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) out.push(compare(a[i]!, b[j]!) <= 0 ? a[i++]! : b[j++]!);
  while (i < a.length) out.push(a[i++]!);
  while (j < b.length) out.push(b[j++]!);
  return out;
}

/** The ordered timeline (module comment). */
export function buildTimeline(input: TimelineInput): TimelineItem[] {
  const { nodes, system } = partsOf(input);
  const items: TimelineItem[] = [];
  for (const entry of input.scope?.entries.values() ?? []) {
    const item = itemOf(entry, input, nodes);
    if (item !== null) items.push(item);
  }
  return foldWaiting(latestRuns(merge(items.sort(compare), system)));
}

interface Built {
  readonly scope: ScopeEvents | undefined;
  readonly floor: number | undefined;
  readonly row: TaskRow;
  readonly sessions: TasksState['sessions'];
  readonly parts: Parts;
  /** Per `scope_seq`: the entry the item was made from and the item (null: the entry shows none). */
  readonly byEntry: Map<number, { readonly entry: EventEntry; readonly item: TimelineItem | null }>;
  /** The event items, sorted. */
  readonly events: TimelineItem[];
  readonly items: TimelineItem[];
}

/** A full build (the task row or the sessions changed). */
function build(input: TimelineInput): Built {
  const parts = partsOf(input);
  const byEntry: Built['byEntry'] = new Map();
  const events: TimelineItem[] = [];
  for (const entry of input.scope?.entries.values() ?? []) {
    const item = itemOf(entry, input, parts.nodes);
    byEntry.set(entry.scopeSeq, { entry, item });
    if (item !== null) events.push(item);
  }
  events.sort(compare);
  return {
    scope: input.scope,
    floor: input.floor,
    row: input.task,
    sessions: input.sessions,
    parts,
    byEntry,
    events,
    items: foldWaiting(latestRuns(merge(events, parts.system))),
  };
}

/**
 * Only the scope's events changed: the entries that are new or replaced are turned into items, sorted among
 * themselves and merged into the previous sorted list (linear), instead of sorting every event again (ADR-086).
 */
function update(last: Built, input: TimelineInput): Built {
  const byEntry: Built['byEntry'] = new Map();
  const added: TimelineItem[] = [];
  const gone = new Set<string>();
  let reused = 0;
  for (const entry of input.scope?.entries.values() ?? []) {
    const known = last.byEntry.get(entry.scopeSeq);
    if (known !== undefined) reused += 1;
    if (known !== undefined && known.entry === entry) {
      byEntry.set(entry.scopeSeq, known);
      continue;
    }
    if (known !== undefined && known.item !== null) gone.add(known.item.key);
    const item = itemOf(entry, input, last.parts.nodes);
    byEntry.set(entry.scopeSeq, { entry, item });
    if (item !== null) added.push(item);
  }
  // entries of the previous build that left the store (evicted or dropped)
  if (reused < last.byEntry.size) {
    for (const [seq, known] of last.byEntry) {
      if (!byEntry.has(seq) && known.item !== null) gone.add(known.item.key);
    }
  }
  const kept = gone.size === 0 ? last.events : last.events.filter((item) => !gone.has(item.key));
  const events = added.length === 0 ? [...kept] : merge(kept, added.sort(compare));
  const unchanged = added.length === 0 && gone.size === 0;
  return {
    ...last,
    scope: input.scope,
    byEntry,
    events: unchanged ? last.events : events,
    items: unchanged ? last.items : foldWaiting(latestRuns(merge(events, last.parts.system))),
  };
}

/**
 * A selector of the timeline of `task` (optionally of one session), memoized on the scope's events, the task row and
 * the session rows, so a component re-renders only when one of them changed; a change of the events alone is merged
 * incrementally (`update`).
 */
export function selectTimeline(wg: string, task: string, session?: string): (state: StoreState) => TimelineItem[] {
  let last: Built | null = null;
  const empty: TimelineItem[] = [];
  const key = pagesKey({ org: '', scope: wg, query: chatQuery(task, session) });
  return (state) => {
    const scope = state.events[wg];
    const row = state.tasks.tasks[task];
    if (row === undefined) return empty;
    const sessions = state.tasks.sessions;
    const floor = boundaryOf(state.pages[key]);
    const same = last !== null && last.row === row && last.sessions === sessions && last.floor === floor;
    if (same && last!.scope === scope) return last!.items;
    const input: TimelineInput = { scope, task: row, sessions, floor, ...(session !== undefined && { session }) };
    last = same ? update(last!, input) : build(input);
    return last.items;
  };
}
