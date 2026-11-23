// Texts and targets of notifications (06-web.md section 6.2). A notification row is content-free (kind, org,
// workgroup, task, a reference ID); the client builds the sentence from the kind and the names it decrypted itself:
//
// | kind | text | target |
// |---|---|---|
// | `whiteboard.entry` | "New whiteboard entry in organization X" / "… in workgroup X" / "… in task Y" | the board |
// | `task.idle` | "Task Y is waiting for you" | the task |
// | `task.failed` | "Task Y failed" | the task |
// | `invite.accepted` | "Z accepted the invitation to X" | the org's members |
// | `rotation.required` | "Key rotation required in organization X" | the org's members |
//
// The level of a whiteboard notification follows from the row: a task → task level, else a workgroup → workgroup
// level, else the org level. Names that are not (yet) decrypted are replaced by neutral fallbacks, never by IDs of
// confidential objects; unknown kinds get a generic text. The names come from verified events only
// (`core/tasks/directory.ts`): `org.profile`, `wg.profile`, `member.profile`, `device.profile` and the task's `task.meta`.
import { isRecord } from '../registry';
import { selectDirectory, selectTitle } from '../tasks/directory';
import type { NotificationRow } from '../store/slices/notifications';
import type { StoreState } from '../store/store';
import { boardPath, orgBoard, taskBoard, wgBoard } from '../whiteboards/location';

export interface Names {
  readonly org: string | null;
  readonly wg: string | null;
  readonly task: string | null;
  /** The subject of `invite.accepted` (a member's display name or a device's label). */
  readonly subject: string | null;
}

export interface Message {
  /** Message key of the `notifications` catalog. */
  readonly key: string;
  readonly params: Readonly<Record<string, string>>;
}

/** The sentence of a notification: message key and parameters, missing names replaced by `fallback`. */
export function notificationMessage(row: NotificationRow, names: Names, fallback: Names): Message {
  const org = names.org ?? fallback.org!;
  const wg = names.wg ?? fallback.wg!;
  const task = names.task ?? fallback.task!;
  const subject = names.subject ?? fallback.subject!;
  switch (row.kind) {
    case 'whiteboard.entry':
      if (row.task_id !== null) return { key: 'notifications.text.whiteboard.task', params: { task, org } };
      if (row.wg_id !== null) return { key: 'notifications.text.whiteboard.workgroup', params: { wg, org } };
      return { key: 'notifications.text.whiteboard.org', params: { org } };
    case 'task.idle':
      return { key: 'notifications.text.task.idle', params: { task, org } };
    case 'task.failed':
      return { key: 'notifications.text.task.failed', params: { task, org } };
    case 'invite.accepted':
      return { key: 'notifications.text.invite.accepted', params: { subject, org } };
    case 'rotation.required':
      return { key: 'notifications.text.rotation.required', params: { org } };
    default:
      return { key: 'notifications.text.unknown', params: { org } };
  }
}

/** Where a click leads (06-web.md section 6.2: "click marks read and navigates"). */
export function notificationTarget(row: NotificationRow): string {
  const org = `/o/${encodeURIComponent(row.org_id)}`;
  switch (row.kind) {
    case 'whiteboard.entry':
      if (row.task_id !== null && row.wg_id !== null) return boardPath(taskBoard(row.org_id, row.wg_id, row.task_id));
      if (row.wg_id !== null) return boardPath(wgBoard(row.org_id, row.wg_id));
      return boardPath(orgBoard(row.org_id));
    case 'task.idle':
    case 'task.failed':
      return row.task_id === null ? org : `${org}/t/${encodeURIComponent(row.task_id)}`;
    case 'invite.accepted':
    case 'rotation.required':
      return `${org}/members`;
    default:
      return org;
  }
}

/** The label of a device from its newest verified `device.profile` in the org scope. */
function deviceLabel(state: StoreState, org: string, device: string): string | null {
  const scope = state.events[org];
  if (scope === undefined) return null;
  let label: string | null = null;
  let seq = -1;
  for (const entry of scope.entries.values()) {
    const header = entry.header;
    if (entry.status !== 'verified' || header!.kind !== 'device.profile' || header!.ref !== device) continue;
    if (entry.scopeSeq > seq && isRecord(entry.body) && typeof entry.body.label === 'string') {
      label = entry.body.label;
      seq = entry.scopeSeq;
    }
  }
  return label;
}

const named = new WeakMap<object, WeakMap<NotificationRow, Names>>();

/**
 * The decrypted names a notification refers to (null where not read yet or not readable); the same object while the
 * `events` slice and the row are unchanged (a stable selector for `useStore`).
 */
export function notificationNames(state: StoreState, row: NotificationRow): Names {
  const byRow = named.get(state.events) ?? new WeakMap<NotificationRow, Names>();
  named.set(state.events, byRow);
  const known = byRow.get(row);
  if (known !== undefined) return known;
  const names = readNames(state, row);
  byRow.set(row, names);
  return names;
}

function readNames(state: StoreState, row: NotificationRow): Names {
  const directory = selectDirectory(row.org_id)(state);
  const title = row.task_id === null || row.wg_id === null ? null : selectTitle(row.wg_id, row.task_id, null)(state);
  const subject =
    row.kind === 'invite.accepted' && row.ref !== null
      ? (directory.members.get(row.ref) ?? deviceLabel(state, row.org_id, row.ref))
      : null;
  return {
    org: directory.orgName,
    wg: row.wg_id === null ? null : (directory.workgroups.get(row.wg_id) ?? null),
    task: title?.status === 'verified' ? title.text : null,
    subject,
  };
}
