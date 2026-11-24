// What the notification center and the bell do (06-web.md section 6.2, 03-api.md section 4.9):
//
// - `loadNotifications`: `GET /v1/notifications?account=1` newest first (`account=1`: also the account-level rows,
//   `agent.account_change` without org) (`next_before` for more); live rows arrive as
//   `notification` frames of the org channel (only those of the active org: the hub filters the stream per org);
// - `readNotification` / `readAll`: `POST /v1/notifications/read` with `{ids:[…]}` or `{all:true}`, then the rows are
//   read in the store and the bell's count drops (answers arriving after a sign-out change nothing);
// - `pullNames`: the profile events of each named org (shared with the task screens) and the title of each named task
//   (`task.meta`), so the texts can name them; `device.profile` events when a device accepted an invitation. Only for
//   orgs whose log is verified (the reader needs it) and workgroups the user reads (others are `not_found`); the
//   user's agents (their names from the sealed labels) once when an account-level row names an agent.
import { loadAgents } from '../agents/list';
import { formatTime } from '../encoding/time';
import { WG } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import {
  ACCOUNT_KIND,
  isNotificationRow,
  markAllRead,
  markRead,
  type NotificationRow,
  putPage,
  setListError,
  setListLoading,
} from '../store/slices/notifications';
import { orgEntry } from '../store/slices/orgs';
import { PROFILE_KINDS } from '../tasks/directory';
import { pullOnce, type TasksContext } from '../tasks/pull';
import { sendErrorCode } from '../tasks/send';

export const PAGE_SIZE = 50;

/** Loads the first page (or with `more` the next one). */
export async function loadNotifications(context: TasksContext, more = false): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const before = more ? store.getState().notifications.list.nextBefore : null;
  if (more && before === null) return;
  setListLoading(store);
  try {
    const params = new URLSearchParams({ account: '1', limit: String(PAGE_SIZE) });
    if (before !== null) params.set('before', before);
    const page = await context.client.request('GET', `/v1/notifications?${params.toString()}`);
    if (!isRecord(page) || !Array.isArray(page.notifications) || !page.notifications.every(isNotificationRow)) {
      throw new HubError('unavailable', 200);
    }
    if (store.generation !== generation) return;
    putPage(store, page.notifications, typeof page.next_before === 'string' ? page.next_before : null);
  } catch (error) {
    if (store.generation === generation) setListError(store, sendErrorCode(error));
  }
}

/** Marks one notification read at the hub, then in the store. */
export async function readNotification(context: TasksContext, id: string): Promise<void> {
  const generation = context.store.generation;
  await context.client.request('POST', '/v1/notifications/read', { body: { ids: [id] } });
  if (context.store.generation === generation) markRead(context.store, [id], formatTime(context.now()));
}

/** Marks every notification read. */
export async function readAll(context: TasksContext): Promise<void> {
  const generation = context.store.generation;
  await context.client.request('POST', '/v1/notifications/read', { body: { all: true } });
  if (context.store.generation === generation) markAllRead(context.store, formatTime(context.now()));
}

/** Pulls the names the texts of `rows` need (module comment); failures leave the fallbacks. */
export async function pullNames(context: TasksContext, rows: readonly NotificationRow[]): Promise<void> {
  const state = context.store.getState();
  const userId = state.me.userId;
  const pulls: Promise<number>[] = [];
  const quiet = (pull: Promise<number>) => pulls.push(pull.catch(() => 0));
  if (rows.some((row) => row.kind === ACCOUNT_KIND) && state.agents.status === 'idle' && state.me.identity !== null) {
    quiet(loadAgents(context).then(() => 0));
  }
  for (const row of rows) {
    if (row.org_id === null) continue;
    const log = orgEntry(state.orgs, row.org_id).log;
    if (log === null || userId === null) continue;
    quiet(pullOnce(context, row.org_id, row.org_id, { kinds: PROFILE_KINDS }));
    if (row.kind === 'invite.accepted') quiet(pullOnce(context, row.org_id, row.org_id, { kinds: ['device.profile'] }));
    if (row.task_id !== null && row.wg_id !== null && log.state.canRead(userId, WG, row.wg_id)) {
      quiet(pullOnce(context, row.org_id, row.wg_id, { kinds: ['task.meta'], task: row.task_id }));
    }
  }
  await Promise.all(pulls);
}
