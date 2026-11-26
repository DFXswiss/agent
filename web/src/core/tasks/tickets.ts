// Captured tasks (E-41, ADR-100): a task is captured like a ticket – title, description, person, topic, GitHub
// organisation and repository, urgency – and an agent starts only when someone starts it.
//
// - The fields the hub sees (section 2): `status`, `urgency`, `assignee_id`, `topic_id` of the task row (absent from an
//   older hub: `taskStatus` derives the status from the state – closed is completed, every other state inProgress –
//   and the urgency is normal). Everything else is sealed in `task.meta` (section 4): `title`, `description`,
//   `github_org`, `github_repo`; a reader takes each field from the newest verified `task.meta` of the task that
//   carries it (a device's `task.set_title` writes only `title`), `selectMeta`. Clients write the full current set.
// - `prepareCapture`/`sendCapture`: `POST /v1/tasks` with the sealed `task.meta` only (no prompt, no device, no
//   placement), with the optional `urgency`, `assignee_id`, `topic_id`; the hub answers `session: null`. The prepared
//   body is kept by the caller: a retry sends the identical request (same IDs, same bytes).
// - `putFields`: `PUT /v1/tasks/{task}/fields` with the changed status, urgency, person or topic (null clears the last
//   two); the answer's row enters the store. `completed` closes the task, `todo`/`inProgress` reopen a closed one.
// - `writeMeta`: a new sealed `task.meta` with the full set of the sealed fields (`POST /v1/events`), read into the
//   store at once; the row is loaded again so that it names the new title event.
// - `startAgent`: the first prompt of the task (`POST /v1/tasks/{task}/prompts`, unchanged route; the hub creates the
//   main session and sets `todo` → `inProgress`), after the device step of the send sequence (`ensureDevice`: a
//   captured task never assigned one); `startPrompt` is its default text: the title, a blank line, the description.
// - Topics (section 3): org-scope `topic.profile` events `{name}`, header `ref` = the topic ID, sealed under the org key
//   like `wg.profile`; any member writes one (`createTopic`). The newest verified event of a `ref` names the topic
//   (`selectTopics`); `loadTopics` pulls them (`GET /v1/scopes/{org}/events?kinds=topic.profile`).
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import { epochKey, ORG, type OrgState, USER } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { memoize } from '../store/selectors';
import type { ScopeEvents } from '../store/slices/events';
import { orgEntry } from '../store/slices/orgs';
import { isTaskRow, putTasks, type TaskRow } from '../store/slices/tasks';
import type { StoreState } from '../store/store';
import { TITLE_KIND } from './directory';
import { rowStatus } from './filters';
import type { TasksContext } from './pull';
import { pullOnce } from './pull';
import { ensureDevice, loadTask, prepareFollowUp, SendError, sealWg, sendFollowUp } from './send';

export const STATUSES = ['todo', 'inProgress', 'completed'] as const;
export const URGENCIES = ['low', 'normal', 'high', 'urgent'] as const;
export type TaskStatus = (typeof STATUSES)[number];
export type Urgency = (typeof URGENCIES)[number];
/** The urgency of a new task and of a row of an older hub. */
export const DEFAULT_URGENCY: Urgency = 'normal';

export const TOPIC_KIND = 'topic.profile';
export const TITLE_MAX = 120;
export const DESCRIPTION_MAX = 4096;
export const TOPIC_MAX = 100;
/** Topic pages pulled at most (newest first). */
export const TOPIC_PAGES = 5;
/** A GitHub login (`body_task_meta.github_org`). */
export const GITHUB_ORG_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
/** `owner/name` (`body_task_meta.github_repo`). */
export const GITHUB_REPO_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/;

const isUrgency = (value: unknown): value is Urgency => (URGENCIES as readonly unknown[]).includes(value);

/** The status of a row: the hub's, or derived from the state for an older hub (closed: completed). */
export const taskStatus = (row: TaskRow): TaskStatus => rowStatus(row) as TaskStatus;

/** The urgency of a row (normal for an older hub). */
export const taskUrgency = (row: TaskRow): Urgency => (isUrgency(row.urgency) ? row.urgency : DEFAULT_URGENCY);

/** The person of a row (null: nobody, or an older hub). */
export const taskAssignee = (row: TaskRow): string | null => row.assignee_id ?? null;

/** The topic of a row (null: none, or an older hub). */
export const taskTopic = (row: TaskRow): string | null => row.topic_id ?? null;

// --- the sealed fields ---

/** The sealed fields of a task (`body_task_meta`); an absent field is "". */
export interface TicketMeta {
  readonly title: string;
  readonly description: string;
  readonly github_org: string;
  readonly github_repo: string;
}

const META_FIELDS = ['title', 'description', 'github_org', 'github_repo'] as const;
export const EMPTY_META: TicketMeta = { title: '', description: '', github_org: '', github_repo: '' };

/** Task ID → its sealed fields, each from the newest verified `task.meta` that carries it. */
const metaOf = memoize((scope: ScopeEvents): ReadonlyMap<string, TicketMeta> => {
  const entries = [...scope.entries.values()]
    .filter((entry) => entry.status === 'verified' && entry.header!.kind === TITLE_KIND)
    .sort((a, b) => a.scopeSeq - b.scopeSeq);
  const byTask = new Map<string, TicketMeta>();
  for (const entry of entries) {
    const task = entry.header!.task as string;
    const known: Record<string, string> = { ...(byTask.get(task) ?? EMPTY_META) };
    for (const field of META_FIELDS) {
      const value = isRecord(entry.body) ? entry.body[field] : undefined;
      if (typeof value === 'string') known[field] = value;
    }
    byTask.set(task, known as unknown as TicketMeta);
  }
  return byTask;
});

/** The sealed fields of `task` in workgroup `wg`, null while no verified `task.meta` of it was read. */
export const selectMeta =
  (wg: string, task: string) =>
  (state: StoreState): TicketMeta | null => {
    const scope = state.events[wg];
    return scope === undefined ? null : (metaOf(scope).get(task) ?? null);
  };

/** The form input of the sealed fields. */
export interface MetaInput {
  readonly title: string;
  readonly description: string;
  readonly githubOrg: string;
  readonly githubRepo: string;
}

/**
 * The `task.meta` body of the input (trimmed; the full set, "" clears a field), or `request_invalid`: a title of 1–120
 * characters on one line, a description of at most 4 096 characters, a GitHub login and an `owner/name` whose owner is
 * the organisation when both are set (any case).
 */
export function metaBody(input: MetaInput): TicketMeta {
  const title = input.title.trim();
  const description = input.description.trim();
  const org = input.githubOrg.trim();
  const repo = input.githubRepo.trim();
  const valid =
    title.length >= 1 &&
    title.length <= TITLE_MAX &&
    !/[\r\n]/.test(title) &&
    description.length <= DESCRIPTION_MAX &&
    (org === '' || GITHUB_ORG_RE.test(org)) &&
    (repo === '' || GITHUB_REPO_RE.test(repo)) &&
    (org === '' || repo === '' || repo.split('/')[0]!.toLowerCase() === org.toLowerCase());
  if (!valid) throw new SendError('request_invalid');
  return { title, description, github_org: org, github_repo: repo };
}

/** The body as sent: every field, the empty optional ones as "" only when `clear` (an edit clears, a capture omits). */
function sealedBody(meta: TicketMeta, clear: boolean): Record<string, string> {
  return Object.fromEntries(META_FIELDS.filter((field) => clear || meta[field] !== '').map((f) => [f, meta[f]]));
}

/** The default text of "Start agent": the title, a blank line and the description. */
export function startPrompt(meta: Pick<TicketMeta, 'title' | 'description'>): string {
  return meta.description.trim() === '' ? meta.title : `${meta.title}\n\n${meta.description.trim()}`;
}

// --- capture ---

/** The hub fields of a task: what `POST /v1/tasks` and `PUT …/fields` take. */
export interface HubFields {
  readonly status?: TaskStatus;
  readonly urgency?: Urgency;
  readonly assignee_id?: string | null;
  readonly topic_id?: string | null;
}

/** A captured task ready to send; keep it to retry with the same IDs. */
export interface PreparedCapture {
  readonly org: string;
  readonly wg: string;
  readonly taskId: string;
  readonly body: {
    readonly task_id: string;
    readonly wg_id: string;
    readonly events: readonly unknown[];
    readonly urgency?: Urgency;
    readonly assignee_id?: string;
    readonly topic_id?: string;
  };
}

/** The user and the verified state of `org` (`forbidden` without `wg:write` in `wg`). */
function writerState(context: TasksContext, org: string, wg: string): OrgState {
  const { userId } = context.store.getState().me;
  const entry = orgEntry(context.store.getState().orgs, org);
  if (userId === null) throw new SendError('unauthorized');
  if (entry.blocked !== null) throw new SendError(entry.blocked.code);
  if (entry.log === null) throw new SendError('log_empty');
  if (!entry.log.state.canWriteWg(userId, wg)) throw new SendError('forbidden');
  return entry.log.state;
}

/** Seals the `task.meta` of a new captured task (module comment). */
export async function prepareCapture(
  context: TasksContext,
  input: {
    readonly org: string;
    readonly wg: string;
    readonly meta: MetaInput;
    readonly urgency?: Urgency;
    readonly assigneeId?: string | null;
    readonly topicId?: string | null;
  },
): Promise<PreparedCapture> {
  writerState(context, input.org, input.wg);
  const meta = metaBody(input.meta);
  if (input.urgency !== undefined && !isUrgency(input.urgency)) throw new SendError('request_invalid');
  const taskId = uuid7(context.now());
  const event = await sealWg(context, input.org, input.wg, taskId, 'task.meta', sealedBody(meta, false));
  return {
    org: input.org,
    wg: input.wg,
    taskId,
    body: {
      task_id: taskId,
      wg_id: input.wg,
      events: [event],
      ...(input.urgency !== undefined && { urgency: input.urgency }),
      ...(input.assigneeId != null && { assignee_id: input.assigneeId }),
      ...(input.topicId != null && { topic_id: input.topicId }),
    },
  };
}

/** Hands own sealed events with the positions the hub gave them to the reader. */
async function readOwn(context: TasksContext, org: string, wg: string, events: readonly unknown[], results: unknown) {
  const items = (Array.isArray(results) ? results : []).flatMap((result, index) =>
    isRecord(result) && result.scope_id === wg && Number.isSafeInteger(result.scope_seq) && index < events.length
      ? [{ scope_seq: result.scope_seq as number, received_at: null, event: events[index] }]
      : [],
  );
  if (items.length > 0) await context.ingest(org, wg, items);
}

/** Sends a prepared capture (send it again unchanged to retry); the row enters the store. */
export async function sendCapture(context: TasksContext, prepared: PreparedCapture): Promise<TaskRow> {
  const answer = await context.client.request('POST', '/v1/tasks', { body: prepared.body });
  if (!isRecord(answer) || !isTaskRow(answer.task) || answer.session !== null) throw new HubError('unavailable', 200);
  if (answer.task.task_id !== prepared.taskId) throw new HubError('unavailable', 200);
  putTasks(context.store, [answer.task]);
  await readOwn(context, prepared.org, prepared.wg, prepared.body.events, answer.events);
  return answer.task;
}

// --- edit ---

/** `PUT /v1/tasks/{task}/fields` with the given hub fields; the answer's row enters the store. */
export async function putFields(context: TasksContext, task: TaskRow, fields: HubFields): Promise<TaskRow> {
  if (Object.keys(fields).length === 0) return task;
  const answer = await context.client.request('PUT', `/v1/tasks/${encodeURIComponent(task.task_id)}/fields`, {
    body: fields,
  });
  if (!isRecord(answer) || !isTaskRow(answer.task) || answer.task.task_id !== task.task_id) {
    throw new HubError('unavailable', 200);
  }
  putTasks(context.store, [answer.task]);
  return answer.task;
}

/** The hub fields of `next` that differ from `row`. */
export function changedFields(row: TaskRow, next: Required<HubFields>): HubFields {
  return {
    ...(next.status !== taskStatus(row) && { status: next.status }),
    ...(next.urgency !== taskUrgency(row) && { urgency: next.urgency }),
    ...(next.assignee_id !== taskAssignee(row) && { assignee_id: next.assignee_id }),
    ...(next.topic_id !== taskTopic(row) && { topic_id: next.topic_id }),
  };
}

/** Whether the sealed fields differ. */
export const metaChanged = (a: TicketMeta, b: TicketMeta): boolean =>
  META_FIELDS.some((field) => a[field] !== b[field]);

/** Writes the full set of the sealed fields as a new `task.meta` (module comment); returns its `scope_seq`. */
export async function writeMeta(context: TasksContext, task: TaskRow, input: MetaInput): Promise<number> {
  writerState(context, task.org_id, task.wg_id);
  const meta = metaBody(input);
  const event = await sealWg(context, task.org_id, task.wg_id, task.task_id, 'task.meta', sealedBody(meta, true));
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== task.wg_id || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  const seq = result.scope_seq as number;
  await context.ingest(task.org_id, task.wg_id, [{ scope_seq: seq, received_at: null, event }]);
  await loadTask(context, task.task_id).catch(() => null);
  return seq;
}

/**
 * "Start agent": `text` as the task's first prompt – after the execution device step of the send sequence (a workgroup
 * without a device gets one where the user may assign it, `ensureDevice`) – returns the new main session's ID and
 * reloads the row. `onAssigning` is called when an assignment starts.
 */
export async function startAgent(
  context: TasksContext,
  task: TaskRow,
  text: string,
  onAssigning?: () => void,
): Promise<string> {
  writerState(context, task.org_id, task.wg_id);
  const event = await prepareFollowUp(context, task, text);
  await ensureDevice(context, task.org_id, task.wg_id, onAssigning);
  const session = await sendFollowUp(context, task, event);
  await loadTask(context, task.task_id).catch(() => null);
  return session;
}

// --- people ---

/**
 * The members that may be a task's person: the active users of the org – people and agent accounts (ADR-071) – by ID.
 * A user's own-identity agent (ADR-082) is no member of its own; the hub refuses it.
 */
export function assignablePeople(state: OrgState): string[] {
  return [...state.principals.values()]
    .filter((p) => p.kind === USER && p.removedSeq === null)
    .sort((a, b) => Number(a.id > b.id) - Number(a.id < b.id))
    .map((p) => p.id);
}

// --- topics ---

/** A topic of the organization. */
export interface Topic {
  readonly id: string;
  readonly name: string;
}

const topicsOf = memoize((scope: ScopeEvents): readonly Topic[] => {
  const names = new Map<string, string>();
  const entries = [...scope.entries.values()]
    .filter((entry) => entry.status === 'verified' && entry.header!.kind === TOPIC_KIND)
    .sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const entry of entries) {
    const name = isRecord(entry.body) ? entry.body.name : undefined;
    if (typeof name === 'string') names.set(entry.header!.ref as string, name);
  }
  return [...names]
    .map(([id, name]) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name) || Number(a.id > b.id) - Number(a.id < b.id));
});

const NO_TOPICS: readonly Topic[] = [];

/** The topics of `org` by name (the newest verified `topic.profile` of each `ref`). */
export const selectTopics =
  (org: string) =>
  (state: StoreState): readonly Topic[] => {
    const scope = state.events[org];
    return scope === undefined ? NO_TOPICS : topicsOf(scope);
  };

/** Pulls the topics of `org` once per sign-in (newest pages first). */
export function loadTopics(context: TasksContext, org: string): Promise<number> {
  return pullOnce(context, org, org, { kinds: [TOPIC_KIND], pages: TOPIC_PAGES });
}

/** The trimmed topic name, or null when it is empty, longer than 100 characters or not one line. */
export function topicName(input: string): string | null {
  const name = input.trim();
  return name.length >= 1 && name.length <= TOPIC_MAX && !/[\r\n]/.test(name) ? name : null;
}

/** Creates a topic (`ref` = a new topic ID) or renames `id`; returns the topic ID. Any member of the org. */
export async function createTopic(context: TasksContext, org: string, input: string, id?: string): Promise<string> {
  const name = topicName(input) ?? fail('request_invalid');
  const { userId, identity } = context.store.getState().me;
  const entry = orgEntry(context.store.getState().orgs, org);
  if (userId === null || identity === null) fail('unauthorized');
  if (entry.blocked !== null) fail(entry.blocked.code);
  const log = entry.log ?? fail('log_empty');
  if (log.state.member(userId) === null) fail('forbidden');
  const epoch = log.state.epoch(ORG, org);
  const key = entry.keys.get(epochKey(ORG, org, epoch)) ?? fail('epoch_stale');
  const now = context.now();
  const ref = id ?? uuid7(now);
  const header = newHeader({
    id: uuid7(now),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: userId,
    kind: TOPIC_KIND,
    ts: formatTime(now),
    lh: log.head.seq,
    ref,
  });
  const event = await sealEvent(key, header, bodyJson({ name }), identity.sig.privateKey);
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== org || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(org, org, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
  return ref;
}

function fail(code: string): never {
  throw new SendError(code);
}
