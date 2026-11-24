// The sessions of a task as a tree (06-web.md section 5.3): main sessions (depth 0, a follow-up after `stopped` or
// `failed` starts a new one, 03-api.md section 4.6.3) with their subagent children by `parent_session_id`, depth ≤ 2
// (ADR-017). Rows come from the `tasks` slice (the countdown keeps sessions there; there is no second session state).
// Children are labelled "Subagent 1", "Subagent 1.2", … by creation order under their parent (the label the chat uses
// to mark subagent output).
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import { memoize } from '../store/selectors';
import type { SessionEntry, TasksState } from '../store/slices/tasks';
import type { StoreState } from '../store/store';

export const MAX_DEPTH = 2;

export interface SessionNode {
  readonly entry: SessionEntry;
  readonly depth: number;
  /** Position path under the main session (`[1, 2]` → "Subagent 1.2"); empty for a main session. */
  readonly path: readonly number[];
  readonly children: readonly SessionNode[];
}

/** Extra fields of `session_row` (kept as received by the `tasks` slice) as far as they are well-formed. */
export interface SessionMeta {
  readonly parent: string | null;
  readonly depth: number;
  readonly modelId: string | null;
}

export function sessionMeta(entry: SessionEntry): SessionMeta {
  const row: Readonly<Record<string, unknown>> = isRecord(entry.row) ? entry.row : {};
  const depth = Number.isSafeInteger(row.depth) ? (row.depth as number) : 0;
  return {
    parent: typeof row.parent_session_id === 'string' ? row.parent_session_id : null,
    depth,
    modelId: typeof row.model_id === 'string' ? row.model_id : null,
  };
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

/** The tree of the task's sessions; children of unknown parents and anything deeper than `MAX_DEPTH` are left out. */
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
    children:
      depth >= MAX_DEPTH
        ? []
        : (children.get(entry.row.session_id) ?? []).map((child, index) =>
            build(child, depth + 1, [...path, index + 1]),
          ),
  });
  return own.filter((entry) => sessionMeta(entry).parent === null).map((entry) => build(entry, 0, []));
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
