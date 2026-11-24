// Texts and targets of notifications (06-web.md section 6.2). A notification row is content-free (kind, org,
// workgroup, task, a reference ID); the client builds the sentence from the kind and the names it decrypted itself:
//
// | kind | text | target |
// |---|---|---|
// | `whiteboard.entry` | "New whiteboard entry in organization X" / "… in workgroup X" / "… in task Y" | the board |
// | `task.idle` | "Task Y is waiting for you" | the task |
// | `task.failed` | "Task Y failed" | the task |
// | `task.attention` | "Task Y needs your decision: <reason>" / "Task Y is stuck: <reason>" (`ref` =
//   `<category>:<reason>:<seq>` or `<category>:<reason>:wg`, KOR-39) | the situation view (the org's home) |
// | `invite.accepted` | "Z accepted the invitation to X" | the org's members |
// | `rotation.required` | "Access in X is being updated" (neutral; the hub no longer creates it, ADR-058) | the org's members |
// | `org.admitted` | "Z admitted you to X" (`ref` = the admitting admin, ADR-060) | the org |
// | `operator.request` | "Organization <id> needs credit" / "… waits for access to the platform's computers" (to
//   operators, no org, `ref` = `<reason>:<org id>`, ADR-079) | the settings |
// | `device.needed` | "Workgroup X in Y needs a computer – assign one" (to admins, ADR-079) | the org's devices |
// | `agent.account_change` | "Agent A removed a passkey" (replaced the recovery code, disconnected an agent, …;
//   `ref` = `<action>:<acting agent id>:<target>`, no org; `agent_paired_remote` (ADR-067): a pairing from another
//   computer was confirmed, named by the new agent, the target) | the settings |
// | `model.switched` | "Task Y: model switched from A to B because A is used up until 14:05" (`ref` =
//   `<from access>:<to access>:<until or empty>`, ADR-081 section 5; a malformed ref: "Task Y: model switched") |
//   the task |
//
// The level of a whiteboard notification follows from the row: a task → task level, else a workgroup → workgroup
// level, else the org level. Names that are not (yet) decrypted are replaced by neutral fallbacks, never by IDs of
// confidential objects; unknown kinds get a generic text. The names come from verified events only
// (`core/tasks/directory.ts`): `org.profile`, `wg.profile`, `member.profile`, `device.profile` and the task's `task.meta`;
// an agent's name from the user's own sealed agent labels (`core/agents/list.ts`).
import { isRecord } from '../registry';
import { reasonKey } from '../attention/view';
import { selectDirectory, selectTitle } from '../tasks/directory';
import { agentName } from '../store/slices/agents';
import { ACCOUNT_KIND, OPERATOR_KIND, type NotificationRow } from '../store/slices/notifications';
import type { StoreState } from '../store/store';
import { boardPath, orgBoard, taskBoard, wgBoard } from '../whiteboards/location';

export interface Names {
  readonly org: string | null;
  readonly wg: string | null;
  readonly task: string | null;
  /** The subject of `invite.accepted` (a member's display name or a device's label) or the admitting admin of
   *  `org.admitted`. */
  readonly subject: string | null;
  /** The name of the agent of an `agent.account_change`. */
  readonly agent: string | null;
}

/** The account-changing actions of an agent the hub reports (`agents/notify.py`). */
export const AGENT_ACTIONS = [
  'passkey_removed',
  'recovery_replaced',
  'recovery_finished',
  'agent_revoked',
  'role_changed',
  'credential_changed',
  'bundle_replaced',
  'invite_created',
  'device_request',
  'agent_paired_remote',
];

/** Action, acting agent and target of an `agent.account_change` row (`ref` = `<action>:<agent id>:<target>`). */
export function agentChange(row: Pick<NotificationRow, 'ref'>): { action: string; agent: string; target: string } {
  const [action = '', agent = '', ...rest] = (row.ref ?? '').split(':');
  return { action, agent, target: rest.join(':') };
}

export interface Message {
  /** Message key of the `notifications` catalog. */
  readonly key: string;
  readonly params: Readonly<Record<string, string>>;
  /** Message key of a further text the UI translates into the parameter `reason` (`task.attention`). */
  readonly reason?: string;
  /** The access kinds of a `model.switched` the UI names as `from` and `to`, and the release it formats as `time`. */
  readonly switched?: ModelSwitched;
}

/** The access kinds a session runs on (`model_access` of the contracts). */
export const SWITCH_ACCESSES = ['subscription', 'api_key', 'platform'];

export interface ModelSwitched {
  readonly from: string;
  readonly to: string;
  /** Unix ms of the release of the used-up access, null without one. */
  readonly until: number | null;
}

const UNTIL_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

/** From, to and release of a `model.switched` row (`ref` = `<from>:<to>:<until or empty>`); null when malformed. */
export function switchedOf(row: Pick<NotificationRow, 'ref'>): ModelSwitched | null {
  const [from = '', to = '', ...rest] = (row.ref ?? '').split(':');
  const text = rest.join(':');
  if (!SWITCH_ACCESSES.includes(from) || !SWITCH_ACCESSES.includes(to) || rest.length === 0) return null;
  if (text === '') return { from, to, until: null };
  const until = UNTIL_RE.test(text) ? Date.parse(text) : NaN;
  return Number.isFinite(until) ? { from, to, until } : null;
}

/** The reasons of an `operator.request` (ADR-079 section 4). */
export const OPERATOR_REASONS = ['credit_exhausted', 'platform_access_required'];

/** Category and reason of a `task.attention` row (`ref` = `<category>:<reason>:<seq|wg>`); null when malformed. */
export function attentionOf(
  row: Pick<NotificationRow, 'ref'>,
): { category: 'needs_decision' | 'blocked'; reason: string } | null {
  const [category, reason] = (row.ref ?? '').split(':');
  if ((category !== 'needs_decision' && category !== 'blocked') || !reason) return null;
  return { category, reason };
}

/** The sentence of a notification: message key and parameters, missing names replaced by `fallback`. */
export function notificationMessage(row: NotificationRow, names: Names, fallback: Names): Message {
  const org = names.org ?? fallback.org!;
  const wg = names.wg ?? fallback.wg!;
  const task = names.task ?? fallback.task!;
  const subject = names.subject ?? fallback.subject!;
  switch (row.kind) {
    case ACCOUNT_KIND: {
      const { action } = agentChange(row);
      const agent = names.agent ?? fallback.agent!;
      const known = AGENT_ACTIONS.includes(action) ? action : 'unknown';
      return { key: `notifications.text.agent.${known}`, params: { agent } };
    }
    case 'whiteboard.entry':
      if (row.task_id !== null) return { key: 'notifications.text.whiteboard.task', params: { task, org } };
      if (row.wg_id !== null) return { key: 'notifications.text.whiteboard.workgroup', params: { wg, org } };
      return { key: 'notifications.text.whiteboard.org', params: { org } };
    case 'task.idle':
      return { key: 'notifications.text.task.idle', params: { task, org } };
    case 'task.failed':
      return { key: 'notifications.text.task.failed', params: { task, org } };
    case 'task.attention': {
      const attention = attentionOf(row);
      if (attention === null) return { key: 'notifications.text.task.attention', params: { task, org } };
      return {
        key: `notifications.text.task.attention.${attention.category}`,
        params: { task, org },
        reason: reasonKey(attention.category, attention.reason),
      };
    }
    case 'invite.accepted':
      return { key: 'notifications.text.invite.accepted', params: { subject, org } };
    case 'rotation.required':
      return { key: 'notifications.text.rotation.required', params: { org } };
    case 'org.admitted':
      return { key: 'notifications.text.org.admitted', params: { subject, org } };
    case OPERATOR_KIND: {
      // the operator is no member of the organization: it is named by its ID (`ref` = `<reason>:<org id>`)
      const [reason = '', id = ''] = (row.ref ?? '').split(':');
      const known = OPERATOR_REASONS.includes(reason) ? reason : 'unknown';
      return { key: `notifications.text.operator.${known}`, params: { org: id } };
    }
    case 'device.needed':
      return { key: 'notifications.text.device.needed', params: { wg, org } };
    case 'model.switched': {
      const switched = switchedOf(row);
      if (switched === null) return { key: 'notifications.text.model.switched', params: { task, org } };
      const key =
        switched.until === null ? 'notifications.text.model.switched.to' : 'notifications.text.model.switched.until';
      return { key, params: { task, org }, switched };
    }
    default:
      return { key: 'notifications.text.unknown', params: { org } };
  }
}

/** Where a click leads (06-web.md section 6.2: "click marks read and navigates"). */
export function notificationTarget(row: NotificationRow): string {
  if (row.org_id === null) return '/settings';
  const org = `/o/${encodeURIComponent(row.org_id)}`;
  switch (row.kind) {
    case 'whiteboard.entry':
      if (row.task_id !== null && row.wg_id !== null) return boardPath(taskBoard(row.org_id, row.wg_id, row.task_id));
      if (row.wg_id !== null) return boardPath(wgBoard(row.org_id, row.wg_id));
      return boardPath(orgBoard(row.org_id));
    case 'task.idle':
    case 'task.failed':
    case 'model.switched':
      return row.task_id === null ? org : `${org}/t/${encodeURIComponent(row.task_id)}`;
    case 'invite.accepted':
    case 'rotation.required':
      return `${org}/members`;
    case 'device.needed':
      return `${org}/devices`;
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

const named = new WeakMap<object, WeakMap<object, WeakMap<NotificationRow, Names>>>();

/**
 * The decrypted names a notification refers to (null where not read yet or not readable); the same object while the
 * `events` and `agents` slices and the row are unchanged (a stable selector for `useStore`).
 */
export function notificationNames(state: StoreState, row: NotificationRow): Names {
  const byAgents = named.get(state.events) ?? new WeakMap<object, WeakMap<NotificationRow, Names>>();
  named.set(state.events, byAgents);
  const byRow = byAgents.get(state.agents) ?? new WeakMap<NotificationRow, Names>();
  byAgents.set(state.agents, byRow);
  const known = byRow.get(row);
  if (known !== undefined) return known;
  const names = readNames(state, row);
  byRow.set(row, names);
  return names;
}

function readNames(state: StoreState, row: NotificationRow): Names {
  if (row.org_id === null) {
    const change = agentChange(row);
    // a confirmed pairing from another computer names the new agent (the target); every other action the acting one
    const subject = change.action === 'agent_paired_remote' ? change.target : change.agent;
    const agent = row.kind === ACCOUNT_KIND ? agentName(state, subject) : null;
    return { org: null, wg: null, task: null, subject: null, agent };
  }
  const directory = selectDirectory(row.org_id)(state);
  const title = row.task_id === null || row.wg_id === null ? null : selectTitle(row.wg_id, row.task_id, null)(state);
  const subject =
    row.ref === null
      ? null
      : row.kind === 'invite.accepted'
        ? (directory.members.get(row.ref) ?? deviceLabel(state, row.org_id, row.ref))
        : row.kind === 'org.admitted'
          ? (directory.members.get(row.ref) ?? null)
          : null;
  return {
    org: directory.orgName,
    wg: row.wg_id === null ? null : (directory.workgroups.get(row.wg_id) ?? null),
    task: title?.status === 'verified' ? title.text : null,
    subject,
    agent: null,
  };
}
