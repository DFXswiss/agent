// The name of a task's main agent (ADR-095 section 2, 06-web.md section 4.0a), read from verified events only:
//
// - `selectAgentName`: the newest verified `task.agent_name` of the task in the workgroup's event store (by
//   `scope_seq`); its body `{name: <text>}` is the given name, `{name: null}` means "automatic", a body that breaks the
//   schema (the hub cannot check sealed bodies) is unverifiable. The task row names the hub's newest name event
//   (`name_event_id`): when the reader could not verify that one, the name is unverifiable unless a newer verified one
//   of the task is known – an unverifiable newest name is never replaced by an older one. A verified event the row
//   names that belongs to another task is ignored (a hub could point at another task's name). When the row names an
//   event the store does not hold, the name is `missing` (it is being pulled), never an older one;
// - `agentLabel`: what is shown – the given name, or the automatic name of the task's title (`name.ts`);
// - `agentNames`: pulls the name events the task rows name and the store does not hold – first every
//   `task.agent_name` of the workgroup once, then, per task and named event, the task's own (a new name event is
//   pulled again, so a rename elsewhere shows up even when the live channel missed it);
// - `renameAgent`: seals `task.agent_name` with the workgroup's current key (`wg:write`, like the title), appends it
//   through `POST /v1/events` and reads it into the store at once.
import { isRecord } from '../registry';
import { HubError } from '../net/signedFetch';
import { memoize, sameAs, scopeIdOf } from '../store/selectors';
import { orgEntry } from '../store/slices/orgs';
import type { ScopeEvents } from '../store/slices/events';
import type { TaskRow } from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import type { Title } from '../tasks/directory';
import { pullEvents, pullOnce, type TasksContext } from '../tasks/pull';
import { sealWg, SendError } from '../tasks/send';
import { automaticName, checkName } from './name';

export const AGENT_NAME_KIND = 'task.agent_name';

/** The name state of a main agent: given, automatic, unverifiable (with the reader's code) or not read yet. */
export type AgentName =
  | { readonly status: 'given'; readonly text: string }
  | { readonly status: 'automatic' }
  | { readonly status: 'unverifiable'; readonly code: string }
  | { readonly status: 'missing' };

/** What is shown: the text of a given or an automatic name, or why there is none. */
export type AgentLabel =
  | { readonly status: 'given' | 'automatic'; readonly text: string }
  | { readonly status: 'unverifiable'; readonly code: string }
  | { readonly status: 'missing' };

/** The schema of a given name (`body_task_agent_name`): 1–80 code points without a line feed or carriage return. */
const NAME_BODY = /^[^\r\n]{1,80}$/u;
const AUTOMATIC: AgentName = { status: 'automatic' };
const MISSING: AgentName = { status: 'missing' };

/** The name of a verified body (`params_invalid` for one that breaks the schema). */
function bodyName(body: unknown): AgentName {
  if (!isRecord(body) || Object.keys(body).length !== 1 || !('name' in body)) {
    return { status: 'unverifiable', code: 'params_invalid' };
  }
  if (body.name === null) return AUTOMATIC;
  return typeof body.name === 'string' && NAME_BODY.test(body.name)
    ? { status: 'given', text: body.name }
    : { status: 'unverifiable', code: 'params_invalid' };
}

interface Indexed {
  readonly seq: number;
  /** The task of a verified event; null for an unverifiable one (its header is not trusted). */
  readonly task: string | null;
  readonly name: AgentName;
}

interface NameIndex {
  readonly byEvent: ReadonlyMap<string, Indexed>;
  /** Task ID → its newest verified name event. */
  readonly byTask: ReadonlyMap<string, Indexed>;
}

/** The newest name index of each scope (weakly: decrypted names do not outlive the store's events; its names are kept
 * when unchanged, `sameAs`). */
const previousNames = new Map<string, WeakRef<NameIndex>>();

const namesOf = memoize((scope: ScopeEvents): NameIndex => {
  const previous = previousNames.get(scopeIdOf(scope) ?? '')?.deref();
  const keep = (id: string, name: AgentName): AgentName => sameAs(previous?.byEvent.get(id)?.name, name);
  const byEvent = new Map<string, Indexed>();
  const byTask = new Map<string, Indexed>();
  for (const entry of scope.entries.values()) {
    if (entry.status === 'unverifiable') {
      const id = isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header.id : null;
      if (typeof id === 'string') {
        byEvent.set(id, {
          seq: entry.scopeSeq,
          task: null,
          name: keep(id, { status: 'unverifiable', code: entry.code! }),
        });
      }
      continue;
    }
    if (entry.status !== 'verified' || entry.header!.kind !== AGENT_NAME_KIND) continue;
    const task = entry.header!.task as string;
    const indexed = { seq: entry.scopeSeq, task, name: keep(entry.header!.id as string, bodyName(entry.body)) };
    byEvent.set(entry.header!.id as string, indexed);
    const known = byTask.get(task);
    if (known === undefined || known.seq < indexed.seq) byTask.set(task, indexed);
  }
  const index = { byEvent, byTask };
  const scopeId = scopeIdOf(scope);
  if (scopeId !== null) previousNames.set(scopeId, new WeakRef(index));
  return index;
});

/** The name of the main agent of `task` in workgroup `wg` (`nameEventId` from the task row; module comment). */
export const selectAgentName =
  (wg: string, task: string, nameEventId: string | null) =>
  (state: StoreState): AgentName => {
    const scope = state.events[wg];
    const index = scope === undefined ? null : namesOf(scope);
    const named = nameEventId === null ? undefined : index?.byEvent.get(nameEventId);
    const newest = index?.byTask.get(task);
    if (named !== undefined && named.task === null && (newest === undefined || newest.seq < named.seq)) {
      return named.name;
    }
    if (newest !== undefined) return newest.name;
    // the row names nothing, or a verified event of another task: nothing was given
    if (nameEventId === null || named !== undefined) return AUTOMATIC;
    return MISSING;
  };

/** Whether the store holds the event `nameEventId` of workgroup `wg` (read, verified or not). */
export const selectNameHeld =
  (wg: string, nameEventId: string) =>
  (state: StoreState): boolean => {
    const scope = state.events[wg];
    return scope !== undefined && namesOf(scope).byEvent.has(nameEventId);
  };

/** What to show for a name and the task's title (an automatic name of an unreadable title is `Agent`). */
export function agentLabel(name: AgentName, title: Title): AgentLabel {
  if (name.status !== 'automatic') return name;
  if (title.status === 'missing') return title;
  return { status: 'automatic', text: automaticName(title.status === 'verified' ? title.text : '') };
}

const pulled = new WeakMap<object, Map<string, Promise<number>>>();

/** Pulls the name events `rows` name that the store does not hold (module comment); failures are tried again. */
export async function agentNames(context: TasksContext, rows: readonly TaskRow[]): Promise<void> {
  const missing = () =>
    rows.filter(
      (row) =>
        typeof row.name_event_id === 'string' &&
        !selectNameHeld(row.wg_id, row.name_event_id)(context.store.getState()),
    );
  const scopes = new Map(missing().map((row) => [row.wg_id, row.org_id]));
  await Promise.all(
    [...scopes].map(([wg, org]) => pullOnce(context, org, wg, { kinds: [AGENT_NAME_KIND] }).catch(() => 0)),
  );
  const store = context.store;
  const pulls = pulled.get(store) ?? new Map<string, Promise<number>>();
  pulled.set(store, pulls);
  await Promise.all(
    missing().map((row) => {
      const key = `${store.generation} ${row.task_id} ${row.name_event_id!}`;
      const known = pulls.get(key);
      if (known !== undefined) return known;
      const pull = pullEvents(context, row.org_id, row.wg_id, { kinds: [AGENT_NAME_KIND], task: row.task_id }).catch(
        () => {
          pulls.delete(key);
          return 0;
        },
      );
      pulls.set(key, pull);
      return pull;
    }),
  );
}

/** Whether the signed-in user may rename the agents of workgroup `wg` (`wg:write` in the verified log). */
export function canRename(state: StoreState, org: string, wg: string): boolean {
  const userId = state.me.userId;
  const log = orgEntry(state.orgs, org).log;
  return userId !== null && log !== null && log.state.canWriteWg(userId, wg);
}

/**
 * Gives the main agent of `task` the name `input` (trimmed, 1–80 code points on one line, `params_invalid` otherwise),
 * or back to the automatic name (`null`). `forbidden` without `wg:write`.
 */
export async function renameAgent(context: TasksContext, task: TaskRow, input: string | null): Promise<void> {
  const name = input === null ? null : checkName(input);
  if (input !== null && name === null) throw new SendError('params_invalid');
  if (!canRename(context.store.getState(), task.org_id, task.wg_id)) throw new SendError('forbidden');
  const event = await sealWg(context, task.org_id, task.wg_id, task.task_id, AGENT_NAME_KIND, { name });
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== task.wg_id || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(task.org_id, task.wg_id, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
}
