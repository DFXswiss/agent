// The sessions of a task as a tree (06-web.md section 5.3): main sessions (depth 0, a follow-up after `stopped` or
// `failed` starts a new one, 03-api.md section 4.6.3) with their subagent children by `parent_session_id`, at any
// depth (the depth is a setting of the platform, ADR-088 section 2). Rows come from the `tasks` slice (the countdown
// keeps sessions there; there is no second session state). Children are labelled "Subagent 1", "Subagent 1.2", … by
// the place the hub gave them below their main agent (`agent_path`, ADR-095 section 1: unique within the task, also
// across main sessions), else – from an older hub – by creation order under their parent (the label the chat uses to
// mark subagent output). With the task's number a node has its handle: `#42` for a main session, `#42.1.2` for a
// subagent (`nodeHandle`). Views indent a row by its depth up to `INDENT_LEVELS` (`indentLevel`); deeper rows keep that
// indentation and their label names the depth.
import { agentPath, mainHandle, subagentHandle } from '../agents/name';
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import { memoize } from '../store/selectors';
import type { SessionEntry, TasksState } from '../store/slices/tasks';
import type { StoreState } from '../store/store';

/** Rows are indented by at most this many levels (module comment). */
export const INDENT_LEVELS = 4;

/** The indentation level of a row of `depth`: the depth, at most `INDENT_LEVELS`. */
export const indentLevel = (depth: number): number => Math.min(Math.max(depth, 0), INDENT_LEVELS);

export interface SessionNode {
  readonly entry: SessionEntry;
  readonly depth: number;
  /** Position path under the main agent (`[1, 2]` → "Subagent 1.2"); empty for a main session. */
  readonly path: readonly number[];
  readonly children: readonly SessionNode[];
}

/** Extra fields of `session_row` (kept as received by the `tasks` slice) as far as they are well-formed. */
export interface SessionMeta {
  readonly parent: string | null;
  readonly depth: number;
  readonly modelId: string | null;
  /** The hub's `agent_path` as ordinals (null when absent or malformed). */
  readonly agentPath: readonly number[] | null;
}

export function sessionMeta(entry: SessionEntry): SessionMeta {
  const row: Readonly<Record<string, unknown>> = isRecord(entry.row) ? entry.row : {};
  const depth = Number.isSafeInteger(row.depth) ? (row.depth as number) : 0;
  const path = agentPath(row);
  return {
    parent: typeof row.parent_session_id === 'string' ? row.parent_session_id : null,
    depth,
    modelId: typeof row.model_id === 'string' ? row.model_id : null,
    agentPath: path === null ? null : path.split('.').map(Number),
  };
}

/** The handle of a node of the task numbered `number` (null without a number). */
export function nodeHandle(node: Pick<SessionNode, 'depth' | 'path'>, number: number | null): string | null {
  if (number === null) return null;
  return node.depth === 0 ? mainHandle(number) : subagentHandle(number, node.path.join('.'));
}

/** Creation time of a row; a malformed one sorts first instead of breaking the tree. */
function created(entry: SessionEntry): number {
  try {
    return parseTime(entry.row.created_at);
  } catch {
    return 0;
  }
}

const byCreation = (a: SessionEntry, b: SessionEntry): number =>
  created(a) - created(b) || Number(a.row.session_id > b.row.session_id) - Number(a.row.session_id < b.row.session_id);

/** The tree of the task's sessions at any depth; children of unknown parents are left out (every node is reached from
 * a main session through its one parent, so the tree has no cycle). */
export function sessionTree(sessions: TasksState['sessions'], task: string): SessionNode[] {
  const own = Object.values(sessions)
    .filter((entry) => entry.row.task_id === task)
    .sort(byCreation);
  const children = new Map<string, SessionEntry[]>();
  for (const entry of own) {
    const parent = sessionMeta(entry).parent;
    if (parent !== null) children.set(parent, [...(children.get(parent) ?? []), entry]);
  }
  const build = (entry: SessionEntry, depth: number, path: readonly number[]): SessionNode => ({
    entry,
    depth,
    path,
    children: (children.get(entry.row.session_id) ?? []).map((child, index) =>
      build(child, depth + 1, sessionMeta(child).agentPath ?? [...path, index + 1]),
    ),
  });
  return own.filter((entry) => sessionMeta(entry).parent === null).map((entry) => build(entry, 0, []));
}

/**
 * The GitHub identity of a session that acts with another one than its task's `taskIdentity` (ADR-095 section 6: a
 * subagent may act as another account), else null.
 */
export function ownIdentity(node: Pick<SessionNode, 'entry'>, taskIdentity: string | null): string | null {
  const own = node.entry.row.github_identity ?? null;
  return own !== null && own !== taskIdentity ? own : null;
}

/**
 * What a node shows below its link (ADR-095 section 6): its own GitHub identity when it acts with another one than the
 * task, and – for a subagent – the operation that requested it (its purpose). Lists, so that views map them.
 */
export function nodeFacts(
  node: Pick<SessionNode, 'entry' | 'depth'>,
  taskIdentity: string | null,
): { readonly identities: readonly string[]; readonly origins: readonly string[] } {
  const identity = ownIdentity(node, taskIdentity);
  // rows in the store passed `isSessionRow`; the operation is one of their extra fields
  const row = node.entry.row as unknown as Readonly<Record<string, unknown>>;
  const origin = node.depth > 0 && typeof row.origin_operation_id === 'string' ? row.origin_operation_id : null;
  return { identities: identity === null ? [] : [identity], origins: origin === null ? [] : [origin] };
}

/** Every node of the tree, depth first. */
export function flatten(nodes: readonly SessionNode[]): SessionNode[] {
  return nodes.flatMap((node) => [node, ...flatten(node.children)]);
}

/** Session ID → its node, for the task. */
export const selectSessionIndex = (task: string) => {
  const index = memoize((sessions: TasksState['sessions']) => {
    const nodes = new Map<string, SessionNode>();
    for (const node of flatten(sessionTree(sessions, task))) nodes.set(node.entry.row.session_id, node);
    return nodes as ReadonlyMap<string, SessionNode>;
  });
  return (state: StoreState) => index(state.tasks.sessions);
};

/** The children of a session in creation order (rows the store holds). */
export function childrenOf(sessions: TasksState['sessions'], session: string): SessionEntry[] {
  return Object.values(sessions)
    .filter((entry) => sessionMeta(entry).parent === session)
    .sort(byCreation);
}
