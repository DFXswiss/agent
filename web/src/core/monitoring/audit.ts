// The audit view (03-api.md section 4.11, 06-web.md section 9.2): filters, pages, the time filter and the chain
// verification. The hub filters by `action` (an action or a dotted group: `operation` matches `operation.*`), `actor`,
// `task`, `session` and `wg`, and pages newest first with the opaque cursor `before`; it has no time parameter
// (decision HUB-16 15), so the time filter is applied here: rows outside `[from, to]` are not shown, and paging stops
// once the oldest row of a page is older than `from` (rows are newest first, nothing older can match).
//
// Filters live in the URL query of the screen (`?action=log&wg=…&from=…`), so a filtered view can be linked; values
// that are not well-formed are dropped before any request.
import { formatTime } from '../encoding/time';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';

/** The closed list of audit actions, a copy of `contracts/enums/audit-actions.json` (a test compares them). */
export const AUDIT_ACTIONS: readonly string[] = [
  'agent.claim',
  'agent.confirm',
  'agent.identity',
  'agent.pairing',
  'agent.register',
  'agent.revoke',
  'audit.repeat',
  'auth.login',
  'auth.passkey_add',
  'auth.passkey_bundle',
  'auth.passkey_remove',
  'auth.recovery',
  'auth.refused',
  'auth.register',
  'auth.session_end',
  'billing.export',
  'billing.fx_set',
  'community.delete',
  'community.key_create',
  'community.key_reject',
  'community.key_request',
  'community.key_wrap',
  'community.moderator_grant',
  'community.moderator_revoke',
  'community.mute',
  'community.post',
  'community.unmute',
  'credential.delete',
  'credential.put',
  'credential.rotate',
  'credit.changed',
  'device.certify',
  'device.connect',
  'device.disconnect',
  'device.enroll',
  'device_request.answered',
  'device_request.delivered',
  'device_request.expired',
  'device_request.failed',
  'device_request.pending',
  'event.append',
  'event.append_batch',
  'github.default',
  'github.primary',
  'hook.denied',
  'invite.create',
  'invite.expire',
  'invite.lookup',
  'invite.register',
  'invite.release',
  'invite.revoke',
  'invite.use',
  'log.accept',
  'log.admit',
  'log.agent_add',
  'log.device_assign',
  'log.device_assign_org',
  'log.device_unassign',
  'log.device_unassign_org',
  'log.genesis',
  'log.grant',
  'log.invite',
  'log.invite_revoke',
  'log.remove',
  'log.role',
  'log.rotate',
  'log.ungrant',
  'log.wg_archive',
  'log.wg_create',
  'model.provider_outside_allowlist',
  'notification.read',
  'operation.accepted',
  'operation.denied',
  'operation.duplicate',
  'operation.executing',
  'operation.expired',
  'operation.failed',
  'operation.received',
  'operation.rejected',
  'operation.succeeded',
  'outbox.rejected',
  'ping.delivered',
  'platform.device_certify',
  'platform.device_decertify',
  'platform.device_invite',
  'platform.org_operator_subscriptions',
  'platform.org_platform_access',
  'platform.platform_policy',
  'platform.role_grant',
  'platform.role_revoke',
  'platform.root',
  'platform.user_certify',
  'platform.user_decertify',
  'platform.user_invite',
  'pr.linked',
  'pr.unlinked',
  'prompt.withdraw',
  'read.agent.handover',
  'read.agent.me',
  'read.community',
  'read.community.messages',
  'read.community.moderators',
  'read.community.mutes',
  'read.community.requests',
  'read.device.context',
  'read.device.context.chunk',
  'read.device.credential',
  'read.device.github_identities',
  'read.device.github_identity',
  'read.device.logs',
  'read.device.platform_log',
  'read.device.pull',
  'read.device.request',
  'read.device.state',
  'read.device.task',
  'read.device.workgroup.tasks',
  'read.device.wraps',
  'read.device_request',
  'read.github_identity',
  'read.me',
  'read.me.agents',
  'read.me.audit',
  'read.me.bundles.recovery',
  'read.me.credential',
  'read.me.credentials',
  'read.me.devices',
  'read.me.github_identities',
  'read.me.invitees',
  'read.me.quotas',
  'read.monitoring.overview',
  'read.monitoring.series',
  'read.notifications',
  'read.operation',
  'read.org',
  'read.org.attention',
  'read.org.attention.summary',
  'read.org.audit',
  'read.org.audit.verify',
  'read.org.billing',
  'read.org.catch_up',
  'read.org.devices',
  'read.org.invites',
  'read.org.log',
  'read.org.quotas',
  'read.platform.enrollments',
  'read.platform.invites',
  'read.platform.log',
  'read.platform.user',
  'read.platform.waiting',
  'read.scope.events',
  'read.session',
  'read.session.get_context',
  'read.session.get_notifications',
  'read.session.get_operation',
  'read.session.get_subagent_result',
  'read.session.get_task',
  'read.session.get_task_messages',
  'read.session.get_whiteboard',
  'read.session.list_members',
  'read.session.list_operations',
  'read.session.list_tasks',
  'read.session.list_workgroups',
  'read.stream',
  'read.stream.browser',
  'read.stream.terminal',
  'read.task',
  'read.task.charges',
  'read.task.contexts',
  'read.task.log',
  'read.task.log.chunk',
  'read.task.operations',
  'read.task.queue',
  'read.user.identity',
  'read.user.lookup',
  'read.whiteboard.history',
  'read.whiteboards',
  'read.workgroup',
  'read.workgroup.charges',
  'read.workgroup.device_candidates',
  'read.workgroup.placement',
  'read.workgroup.pr_link',
  'read.workgroup.pr_links',
  'read.workgroup.tasks',
  'read.wraps',
  'request.refused',
  'request.write',
  'runner.alert',
  'runner.inbound_rejected',
  'runner.log_fork',
  'runner.log_rollback',
  'runner.operator_github_users',
  'session.agent_starting',
  'session.context_fallback',
  'session.context_refused',
  'session.context_restored',
  'session.context_saved',
  'session.control',
  'session.failed',
  'session.idle',
  'session.model_switched',
  'session.pr_linked',
  'session.prompt_delivering',
  'session.prompt_refused',
  'session.prompt_withdrawn',
  'session.requested',
  'session.resuming',
  'session.sandbox_ready',
  'session.stopped',
  'session.stopping',
  'session.suspended',
  'session.vm_assigning',
  'session.vm_ready',
  'session.working',
  'settings.put',
  'task.close',
  'task.create',
  'task.github_identity',
  'task.model_consent',
  'task.placement',
  'wg.placement',
  'whiteboard.notify',
  'wrap.put',
];

/** The action groups (the part before the first dot), sorted. */
export const ACTION_GROUPS: readonly string[] = [
  ...new Set(AUDIT_ACTIONS.map((action) => action.split('.')[0]!)),
].sort();

/** Rows per page (the hub allows up to 200). */
export const AUDIT_PAGE = 100;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ACTION = /^[a-z_]+(?:\.[a-z_]+)*$/;
const MAX_ACTION = 96;

export const ID_FILTERS = ['actor', 'wg', 'task', 'session'] as const;
export type IdFilter = (typeof ID_FILTERS)[number];

export interface AuditFilters {
  readonly action: string | null;
  readonly actor: string | null;
  readonly wg: string | null;
  readonly task: string | null;
  readonly session: string | null;
  /** Unix ms, inclusive bounds of the time filter. */
  readonly from: number | null;
  readonly to: number | null;
}

export const NO_FILTERS: AuditFilters = {
  action: null,
  actor: null,
  wg: null,
  task: null,
  session: null,
  from: null,
  to: null,
};

export const isUuid = (value: string): boolean => UUID.test(value);
export const isAction = (value: string): boolean => value.length <= MAX_ACTION && ACTION.test(value);

function time(text: string | null): number | null {
  if (text === null || text === '') return null;
  const at = Date.parse(text);
  return Number.isNaN(at) ? null : at;
}

/** The filters of a URL query (`URLSearchParams`); malformed values are dropped. */
export function parseFilters(query: { get(name: string): string | null }): AuditFilters {
  const id = (name: IdFilter): string | null => {
    const value = query.get(name);
    return value !== null && isUuid(value) ? value : null;
  };
  const action = query.get('action');
  return {
    action: action !== null && isAction(action) ? action : null,
    actor: id('actor'),
    wg: id('wg'),
    task: id('task'),
    session: id('session'),
    from: time(query.get('from')),
    to: time(query.get('to')),
  };
}

/** The URL query of `filters` (only the set ones; times as UTC header times). */
export function filterQuery(filters: AuditFilters): string {
  const query = new URLSearchParams();
  if (filters.action !== null) query.set('action', filters.action);
  for (const name of ID_FILTERS) {
    const value = filters[name];
    if (value !== null) query.set(name, value);
  }
  if (filters.from !== null) query.set('from', formatTime(filters.from));
  if (filters.to !== null) query.set('to', formatTime(filters.to));
  return query.toString();
}

/** The filters the hub applies, as its query (the store key of a view together with the org). */
export function hubQuery(filters: AuditFilters): string {
  return filterQuery({ ...filters, from: null, to: null });
}

/** `GET /v1/orgs/{org}/audit` for a page (`before`: the cursor of the previous page). */
export function auditTarget(org: string, filters: AuditFilters, before: string | null): string {
  const query = new URLSearchParams(hubQuery(filters));
  query.set('limit', String(AUDIT_PAGE));
  if (before !== null) query.set('before', before);
  return `/v1/orgs/${encodeURIComponent(org)}/audit?${query.toString()}`;
}

export type AuditDetail = Readonly<Record<string, string | number | boolean | null>>;

export interface AuditRow {
  readonly seq: number;
  readonly at: string;
  readonly actor_id: string | null;
  readonly actor_kind: string;
  readonly action: string;
  readonly result: string;
  readonly org_id: string | null;
  readonly wg_id: string | null;
  readonly task_id: string | null;
  readonly session_id: string | null;
  readonly device_id: string | null;
  readonly target_kind: string | null;
  readonly target_id: string | null;
  readonly detail: AuditDetail;
  readonly event_id: string | null;
  /** The agent whose request wrote the row (11-agent-access.md section 7); absent otherwise. */
  readonly agent_id?: string;
}

const NULLABLE_TEXT = [
  'actor_id',
  'org_id',
  'wg_id',
  'task_id',
  'session_id',
  'device_id',
  'target_kind',
  'target_id',
  'event_id',
] as const;

const isDetailValue = (value: unknown): boolean =>
  value === null || ['string', 'number', 'boolean'].includes(typeof value);

export function isAuditRow(value: unknown): value is AuditRow {
  return (
    isRecord(value) &&
    Number.isSafeInteger(value.seq) &&
    typeof value.at === 'string' &&
    !Number.isNaN(Date.parse(value.at)) &&
    ['actor_kind', 'action', 'result'].every((field) => typeof value[field] === 'string') &&
    NULLABLE_TEXT.every((field) => value[field] === null || typeof value[field] === 'string') &&
    isRecord(value.detail) &&
    Object.values(value.detail).every(isDetailValue) &&
    (value.agent_id === undefined || typeof value.agent_id === 'string')
  );
}

export interface AuditPage {
  readonly rows: readonly AuditRow[];
  readonly next: string | null;
}

/** A page of the audit list; a malformed body is `unavailable`, malformed rows are left out. */
export function parseAuditPage(body: unknown): AuditPage {
  if (!isRecord(body) || !Array.isArray(body.rows)) throw new HubError('unavailable', 200);
  const next = typeof body.next_before === 'string' ? body.next_before : null;
  return { rows: body.rows.filter(isAuditRow), next };
}

/** `GET /v1/me/audit` (the user's account audit, 11-agent-access.md section 7) for a page. */
export function accountAuditTarget(before: string | null): string {
  const query = new URLSearchParams({ limit: String(AUDIT_PAGE) });
  if (before !== null) query.set('before', before);
  return `/v1/me/audit?${query.toString()}`;
}

/** Whether the row is inside the time filter. */
export function inTime(row: AuditRow, filters: AuditFilters): boolean {
  const at = Date.parse(row.at);
  return (filters.from === null || at >= filters.from) && (filters.to === null || at <= filters.to);
}

/** Whether no further page can hold a row of the time filter (the oldest row is already older than `from`). */
export function pastRange(rows: readonly AuditRow[], filters: AuditFilters): boolean {
  const oldest = rows.at(-1);
  return filters.from !== null && oldest !== undefined && Date.parse(oldest.at) < filters.from;
}

export interface Checkpoint {
  readonly hub_key_id: string;
  readonly seq: number;
  readonly hash: string;
  readonly published_at: string;
}

export interface VerifyResult {
  readonly ok: boolean;
  readonly head: { readonly seq: number; readonly hash: string };
  readonly checkpoint: Checkpoint | null;
}

const isPosition = (value: unknown): value is { seq: number; hash: string } =>
  isRecord(value) && Number.isSafeInteger(value.seq) && typeof value.hash === 'string';

const isCheckpoint = (value: unknown): value is Checkpoint =>
  isPosition(value) &&
  typeof (value as Record<string, unknown>).hub_key_id === 'string' &&
  typeof (value as Record<string, unknown>).published_at === 'string';

/** The body of `GET /v1/orgs/{org}/audit/verify`; a malformed one is `unavailable`. */
export function parseVerify(body: unknown): VerifyResult {
  if (!isRecord(body) || typeof body.ok !== 'boolean' || !isPosition(body.head)) {
    throw new HubError('unavailable', 200);
  }
  const point = body.checkpoint;
  if (point !== null && !isCheckpoint(point)) throw new HubError('unavailable', 200);
  const checkpoint =
    point === null
      ? null
      : { hub_key_id: point.hub_key_id, seq: point.seq, hash: point.hash, published_at: point.published_at };
  return { ok: body.ok, head: { seq: body.head.seq, hash: body.head.hash }, checkpoint };
}

/** Where the referenced event of a row is shown: the session detail, else the task page; null without one. */
export function eventLink(org: string, row: AuditRow): string | null {
  if (row.event_id === null) return null;
  if (row.session_id !== null) return `/o/${org}/s/${row.session_id}`;
  if (row.task_id !== null) return `/o/${org}/t/${row.task_id}`;
  return null;
}

/** Names the client can decrypt (verified profiles of the org, device labels, task titles); null where unknown. */
export interface AuditNames {
  readonly member: (id: string) => string | null;
  readonly device: (id: string) => string | null;
}

/** The display name of the row's actor (users and devices of the org), null when not known. */
export function actorName(row: AuditRow, names: AuditNames): string | null {
  if (row.actor_id === null) return null;
  if (row.actor_kind === 'user') return names.member(row.actor_id);
  if (row.actor_kind === 'device') return names.device(row.actor_id);
  return null;
}

/**
 * The agent marker of a row written by an agent request (06-web.md section 8.3): `undefined` for a row without agent,
 * else the agent's name when it is one of the user's own agents (`agentName`), null otherwise ("by agent").
 */
export function agentMarker(row: AuditRow, agentName: (id: string) => string | null): string | null | undefined {
  return row.agent_id === undefined ? undefined : agentName(row.agent_id);
}

/** The decrypted name of the row's target (a user or a device), null when not known or of another kind. */
export function targetName(row: AuditRow, names: AuditNames): string | null {
  if (row.target_id === null) return null;
  if (row.target_kind === 'user') return names.member(row.target_id);
  if (row.target_kind === 'device') return names.device(row.target_id);
  return null;
}
