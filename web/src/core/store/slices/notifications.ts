// Slice `notifications` (06-web.md section 6.2, 03-api.md sections 4.9 and 5, ADR-020): the user's content-free
// notification rows (`GET /v1/notifications` pages and live `notification` frames), the load state of the list, the
// unread count of the bell and the live toasts.
//
// Unread count: `GET /v1/me` carries `unread_notifications`; a live frame of a new unread row adds one, marking read
// subtracts. The adjusted count remembers the profile count it started from (`base`): as long as the profile reports
// that same number the adjusted count wins (a profile copy with unchanged count, e.g. after saving the language), a
// different number from the hub replaces it.
import { isRecord } from '../../registry';
import type { FrameHandlers } from '../../net/frames';
import type { Slice, Store, StoreState } from '../store';

export const NOTIFICATION_KINDS = [
  'whiteboard.entry',
  'task.idle',
  'task.failed',
  'task.attention',
  'invite.accepted',
  'rotation.required',
  'agent.account_change',
  'org.admitted',
  'operator.request',
  'device.needed',
  'model.switched',
] as const;

/** The account-level kind (no org): an account-changing action of one of the user's agents (11-agent-access.md
 *  section 8); `ref` is `<action>:<agent id>`. */
export const ACCOUNT_KIND = 'agent.account_change';
/** The other account-level kind: a request to the operator (ADR-079); `ref` is `<reason>:<org id>`. */
export const OPERATOR_KIND = 'operator.request';

/** At most this many toasts at a time (the oldest leaves first). */
export const MAX_TOASTS = 3;

/** One row (`notification_row` of `contracts/api/objects.json`). */
export interface NotificationRow {
  readonly notification_id: string;
  readonly kind: string;
  /** Null only for the account-level kind `agent.account_change`. */
  readonly org_id: string | null;
  readonly wg_id: string | null;
  readonly task_id: string | null;
  readonly ref: string | null;
  readonly created_at: string;
  readonly read_at: string | null;
}

export interface NotificationList {
  readonly status: 'idle' | 'loading' | 'ready' | 'error';
  readonly code: string | null;
  readonly nextBefore: string | null;
}

export interface NotificationsState {
  readonly rows: Readonly<Record<string, NotificationRow>>;
  readonly list: NotificationList;
  /** The adjusted unread count and the profile count it is based on (module comment); null: the profile's. */
  readonly unread: { readonly count: number; readonly base: number } | null;
  /** Notification IDs of the open toasts, oldest first. */
  readonly toasts: readonly string[];
}

declare module '../store' {
  interface StoreState {
    notifications: NotificationsState;
  }
}

export const slice: Slice<'notifications'> = {
  name: 'notifications',
  initial: () => ({ rows: {}, list: { status: 'idle', code: null, nextBefore: null }, unread: null, toasts: [] }),
};

const isId = (value: unknown): value is string => typeof value === 'string' && value !== '';
const isIdOrNull = (value: unknown): boolean => value === null || isId(value);

export function isNotificationRow(value: unknown): value is NotificationRow {
  return (
    isRecord(value) &&
    isId(value.notification_id) &&
    isId(value.kind) &&
    (isId(value.org_id) || (value.org_id === null && (value.kind === ACCOUNT_KIND || value.kind === OPERATOR_KIND))) &&
    isIdOrNull(value.wg_id) &&
    isIdOrNull(value.task_id) &&
    isIdOrNull(value.ref) &&
    typeof value.created_at === 'string' &&
    (value.read_at === null || typeof value.read_at === 'string')
  );
}

/** `unread_notifications` of the `GET /v1/me` body (0 when absent). */
export function profileUnread(state: StoreState): number {
  const count = state.me.profile?.unread_notifications;
  return Number.isSafeInteger(count) && (count as number) >= 0 ? (count as number) : 0;
}

/** The bell's count (module comment). */
export function selectUnread(state: StoreState): number {
  const base = profileUnread(state);
  const unread = state.notifications.unread;
  return unread !== null && unread.base === base ? unread.count : base;
}

function withUnread(state: StoreState, notifications: NotificationsState, delta: number | 'zero'): NotificationsState {
  const count = delta === 'zero' ? 0 : Math.max(0, selectUnread(state) + delta);
  return { ...notifications, unread: { count, base: profileUnread(state) } };
}

/** Rows newest first (`created_at`, then ID, both descending). */
export function sortedRows(rows: Readonly<Record<string, NotificationRow>>): NotificationRow[] {
  return Object.values(rows).sort(
    (a, b) =>
      Number(b.created_at > a.created_at) - Number(b.created_at < a.created_at) ||
      Number(b.notification_id > a.notification_id) - Number(b.notification_id < a.notification_id),
  );
}

export function setListLoading(store: Store): void {
  store.update('notifications', (state) => ({ ...state, list: { ...state.list, status: 'loading', code: null } }));
}

/** A page of the list; rows already known are replaced by the hub's version (`read_at` may have changed). */
export function putPage(store: Store, rows: readonly NotificationRow[], nextBefore: string | null): void {
  store.update('notifications', (state) => {
    const next = { ...state.rows };
    for (const row of rows) next[row.notification_id] = row;
    return { ...state, rows: next, list: { status: 'ready', code: null, nextBefore } };
  });
}

export function setListError(store: Store, code: string): void {
  store.update('notifications', (state) => ({ ...state, list: { ...state.list, status: 'error', code } }));
}

/** A live notification: a new unread row counts and opens a toast; a known row is only updated. */
export function receive(store: Store, data: unknown): void {
  if (!isNotificationRow(data)) return;
  const whole = store.getState();
  store.update('notifications', (state) => {
    const known = Object.hasOwn(state.rows, data.notification_id);
    const rows = { ...state.rows, [data.notification_id]: data };
    if (known || data.read_at !== null) return { ...state, rows };
    const toasts = [...state.toasts, data.notification_id].slice(-MAX_TOASTS);
    return { ...withUnread(whole, state, 1), rows, toasts };
  });
}

/** Marks rows read (the hub accepted `POST /v1/notifications/read`); the count drops by the rows that were unread. */
export function markRead(store: Store, ids: readonly string[], at: string): void {
  const whole = store.getState();
  store.update('notifications', (state) => {
    const unread = ids.filter((id) => state.rows[id]?.read_at === null);
    if (unread.length === 0) return state;
    const rows = { ...state.rows };
    for (const id of unread) rows[id] = { ...rows[id]!, read_at: at };
    return { ...withUnread(whole, state, -unread.length), rows };
  });
}

/** Every row read and the count 0 (`{all:true}`). */
export function markAllRead(store: Store, at: string): void {
  const whole = store.getState();
  store.update('notifications', (state) => {
    const rows = Object.fromEntries(
      Object.entries(state.rows).map(([id, row]) => [id, row.read_at === null ? { ...row, read_at: at } : row]),
    );
    return { ...withUnread(whole, state, 'zero'), rows };
  });
}

export function dismissToast(store: Store, id: string): void {
  store.update('notifications', (state) =>
    state.toasts.includes(id) ? { ...state, toasts: state.toasts.filter((item) => item !== id) } : state,
  );
}

/** Live frames of the org channel (registration of `core/net/frames.ts`). */
export const frames: FrameHandlers = {
  notification: (store, data) => receive(store, data),
};
