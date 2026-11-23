// A notification row for the tests (`notification_row` of the API); the last character of the ID gives the second.
import type { NotificationRow } from '../../../src/core/store/slices/notifications';

export const notification = (id: string, overrides: Partial<NotificationRow> = {}): NotificationRow => ({
  notification_id: id,
  kind: 'whiteboard.entry',
  org_id: '0190f000-0000-7000-8000-00000000000a',
  wg_id: null,
  task_id: null,
  ref: '0190f000-0000-7000-8000-0000000000e1',
  created_at: `2026-11-21T12:00:0${id.slice(-1)}.000Z`,
  read_at: null,
  ...overrides,
});
