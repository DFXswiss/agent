// What the chat does (06-web.md section 5.2, 03-api.md section 4.6):
//
// - `loadChat`: the task row with its sessions (`GET /v1/tasks/{task}`) and the newest page of the task's chat events
//   (`GET /v1/scopes/{wg}/events?task=…&before=…`, `core/tasks/pages.ts`: again only when the reader skipped bulk
//   events of the task meanwhile; live events arrive over the channel); `loadEarlierChat` reads the next older page
//   ("Earlier"), `chatList` names the list for `selectPages` (06-web.md section 5.2, ADR-063 section 1);
// - `sendPrompt`: a further `prompt` (`POST /v1/tasks/{task}/prompts`; the hub resumes or restarts the main session,
//   section 4.6.3). After a failure a retry can fix, the sealed event is kept and the unchanged text re-sends it;
// - `controlSession`: `interrupt` (Stop) or `stop` (End session) of a session (`POST /v1/sessions/{id}/control`);
// - `closeTask`: `POST /v1/tasks/{task}/close`, the answer's task row into the store.
//
// The rights (`task:write` = `wg:write` on the task's workgroup) are checked by the UI before it offers a control
// (section 1: not rendered without the right) and again by the hub.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { show } from '../store/focus';
import { keepPrompt, setLoad } from '../store/slices/chat';
import { isTaskRow, putTasks, type TaskRow } from '../store/slices/tasks';
import { loadEarlier, openNewest, type PagedList, selectPages } from '../tasks/pages';
import type { TasksContext } from '../tasks/pull';
import { loadTask, prepareFollowUp, sendErrorCode, sendFollowUp } from '../tasks/send';

/** The kinds of the chat's pull (everything a chat item may show; `session.terminal` is never pulled here). */
export const CHAT_KINDS = [
  'prompt',
  'session.output',
  'session.tool',
  'session.file',
  'session.hook',
  'session.status',
  'operation.params',
  'operation.result',
] as const;

/** Failures after which the same sealed prompt is sent again (the composer of the home screen uses the same set). */
export const RETRYABLE: ReadonlySet<string> = new Set(['unavailable', 'rate_limited']);

export type ControlAction = 'interrupt' | 'stop';

/** The paged chat events of a task (with `session`: of one session, the session chat). */
export function chatList(org: string, wg: string, task: string, session?: string): PagedList {
  return { org, scope: wg, query: { kinds: CHAT_KINDS, task, ...(session !== undefined && { session }) } };
}

/**
 * A mounted chat view (task or session chat): its task/session counts as shown for the event reader (`focus.ts`), its
 * newest page is read, and read again after the live channel's `resync`. The returned function ends the view.
 */
export function openChat(context: TasksContext, list: PagedList): () => void {
  const release = show(context.store, {
    scope: list.scope,
    ...(list.query.task !== undefined && { task: list.query.task }),
    ...(list.query.session !== undefined && { session: list.query.session }),
    refresh: () => openNewest(context, list, true),
  });
  void openNewest(context, list);
  return release;
}

/** Reads the next older page of the chat (06-web.md section 5.2: "Earlier"). */
export function loadEarlierChat(context: TasksContext, list: PagedList): Promise<void> {
  return loadEarlier(context, list);
}

/** Loads the task and the newest page of its chat events; the load state is kept per task in the `chat` slice. */
export async function loadChat(context: TasksContext, task: string): Promise<TaskRow | null> {
  setLoad(context.store, task, 'loading');
  try {
    const row = await loadTask(context, task);
    const list = chatList(row.org_id, row.wg_id, task);
    await openNewest(context, list);
    const page = selectPages(list)(context.store.getState());
    if (page.status === 'error') throw new HubError(page.code!, 0);
    setLoad(context.store, task, 'ready');
    return row;
  } catch (error) {
    setLoad(context.store, task, 'error', sendErrorCode(error));
    return null;
  }
}

/** Sends `text` as a further prompt of `task`; returns the session the hub routed it to. */
export async function sendPrompt(context: TasksContext, task: TaskRow, text: string): Promise<string> {
  const kept = context.store.getState().chat.kept[task.task_id];
  const event = kept !== undefined && kept.text === text ? kept.event : await prepareFollowUp(context, task, text);
  keepPrompt(context.store, task.task_id, { text, event });
  try {
    const session = await sendFollowUp(context, task, event);
    keepPrompt(context.store, task.task_id, null);
    return session;
  } catch (error) {
    if (!RETRYABLE.has(sendErrorCode(error))) keepPrompt(context.store, task.task_id, null);
    throw error;
  }
}

export async function controlSession(context: TasksContext, session: string, action: ControlAction): Promise<void> {
  await context.client.request('POST', `/v1/sessions/${encodeURIComponent(session)}/control`, { body: { action } });
}

export async function closeTask(context: TasksContext, task: string): Promise<void> {
  const answer = await context.client.request('POST', `/v1/tasks/${encodeURIComponent(task)}/close`, { body: {} });
  if (!isRecord(answer) || !isTaskRow(answer.task)) throw new HubError('unavailable', 200);
  putTasks(context.store, [answer.task]);
}
