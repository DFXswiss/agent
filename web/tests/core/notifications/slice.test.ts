// The notifications slice: row checks, live frames (new unread rows count and open a toast, known or read rows do
// not), the unread count against the profile's (adjusted count wins while the profile count is unchanged), marking
// read, mark all, toasts, list states and the frame registration.
import { describe, expect, it } from 'vitest';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import { createStore } from '../../../src/core/store';
import { setProfile } from '../../../src/core/store/slices/me';
import {
  dismissToast,
  frames,
  isNotificationRow,
  markAllRead,
  markRead,
  MAX_TOASTS,
  profileUnread,
  putPage,
  receive,
  selectUnread,
  setListError,
  setListLoading,
  sortedRows,
} from '../../../src/core/store/slices/notifications';
import { notification } from './rows';

describe('rows', () => {
  it('checks the row shape', () => {
    expect(isNotificationRow(notification('n1'))).toBe(true);
    // org-less rows exist only for the account-level kind (11-agent-access.md section 8)
    expect(isNotificationRow(notification('n1', { kind: 'agent.account_change', org_id: null }))).toBe(true);
    expect(isNotificationRow(notification('n1', { org_id: null }))).toBe(false);
    expect(isNotificationRow(notification('n1', { read_at: '2026-11-21T12:00:00.000Z' }))).toBe(true);
    expect(isNotificationRow({ ...notification('n1'), wg_id: 3 })).toBe(false);
    expect(isNotificationRow({ ...notification('n1'), read_at: 3 })).toBe(false);
    expect(isNotificationRow({ ...notification('n1'), notification_id: '' })).toBe(false);
    expect(isNotificationRow('n1')).toBe(false);
  });

  it('sorts newest first with the ID as tie-breaker', () => {
    const rows = {
      a: notification('a1'),
      b: notification('b2'),
      c: notification('c2'),
    };
    expect(sortedRows(rows).map((row) => row.notification_id)).toEqual(['c2', 'b2', 'a1']);
  });
});

describe('unread count', () => {
  it('starts from the profile and counts new unread live rows once', () => {
    const store = createStore();
    expect(selectUnread(store.getState())).toBe(0);
    setProfile(store, { unread_notifications: 2 });
    expect(profileUnread(store.getState())).toBe(2);
    receive(store, notification('n1'));
    receive(store, notification('n1'));
    receive(store, notification('n2', { read_at: '2026-11-21T12:00:00.000Z' }));
    receive(store, { nonsense: true });
    expect(selectUnread(store.getState())).toBe(3);
    expect(store.getState().notifications.toasts).toEqual(['n1']);
    // a profile copy with the same count keeps the adjusted count, a new count from the hub replaces it
    setProfile(store, { unread_notifications: 2, settings: {} });
    expect(selectUnread(store.getState())).toBe(3);
    setProfile(store, { unread_notifications: 5 });
    expect(selectUnread(store.getState())).toBe(5);
    setProfile(store, { unread_notifications: -1 });
    expect(selectUnread(store.getState())).toBe(0);
  });

  it('drops by the rows marked read and to zero with mark all', () => {
    const store = createStore();
    setProfile(store, { unread_notifications: 1 });
    putPage(store, [notification('n1'), notification('n2'), notification('n3', { read_at: 'x' })], null);
    markRead(store, ['n1', 'n3', 'unknown'], '2026-11-21T13:00:00.000Z');
    expect(store.getState().notifications.rows.n1!.read_at).toBe('2026-11-21T13:00:00.000Z');
    expect(store.getState().notifications.rows.n3!.read_at).toBe('x');
    expect(selectUnread(store.getState())).toBe(0);
    const state = store.getState().notifications;
    markRead(store, ['n1'], 'later');
    expect(store.getState().notifications).toBe(state);
    receive(store, notification('n4'));
    expect(selectUnread(store.getState())).toBe(1);
    markAllRead(store, 'end');
    expect(selectUnread(store.getState())).toBe(0);
    expect(Object.values(store.getState().notifications.rows).every((row) => row.read_at !== null)).toBe(true);
    expect(store.getState().notifications.rows.n3!.read_at).toBe('x');
  });
});

describe('toasts and list', () => {
  it('keeps the newest toasts and dismisses them', () => {
    const store = createStore();
    for (let index = 1; index <= MAX_TOASTS + 1; index += 1) receive(store, notification(`n${index}`));
    expect(store.getState().notifications.toasts).toEqual(['n2', 'n3', 'n4']);
    dismissToast(store, 'n3');
    expect(store.getState().notifications.toasts).toEqual(['n2', 'n4']);
    const state = store.getState().notifications;
    dismissToast(store, 'n3');
    expect(store.getState().notifications).toBe(state);
  });

  it('tracks the list state', () => {
    const store = createStore();
    setListLoading(store);
    expect(store.getState().notifications.list.status).toBe('loading');
    setListError(store, 'unavailable');
    expect(store.getState().notifications.list).toEqual({ status: 'error', code: 'unavailable', nextBefore: null });
    putPage(store, [notification('n1')], 'next');
    expect(store.getState().notifications.list).toEqual({ status: 'ready', code: null, nextBefore: 'next' });
  });

  it('registers the notification frame', () => {
    const store = createStore();
    expect(frameHandlers().some((set) => set === frames)).toBe(true);
    expect(applyFrame(store, 'notification', notification('n1'), 0)).toBeGreaterThan(0);
    expect(store.getState().notifications.rows.n1).toBeDefined();
  });
});
