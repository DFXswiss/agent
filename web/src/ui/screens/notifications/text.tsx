// The sentence of a notification for the center and the toasts: kind + decrypted names (`core/notifications/text.ts`),
// neutral fallbacks while a name is not read yet; the names are pulled once per row set. The center reads the names
// of all its rows with one selector (`namesSelector`), a toast with its own (`NotificationText`).
import { useEffect } from 'react';
import { pullNames } from '../../../core/notifications/actions';
import { type Names, notificationMessage, notificationNames } from '../../../core/notifications/text';
import type { Selector } from '../../../core/store';
import type { NotificationRow } from '../../../core/store/slices/notifications';
import { cachedSelector } from '../audit/select';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

/** Pulls the names `rows` refer to (profiles, device labels, task titles). */
export function useNames(rows: readonly NotificationRow[]): void {
  const { app } = useAppEnv();
  useEffect(() => {
    if (rows.length > 0) void pullNames(app.tasks, rows);
  }, [app, rows]);
}

/** Notification ID → the names of its text for every row; recomputed only when the `events` or `agents` slice changed. */
export function namesSelector(rows: readonly NotificationRow[]): Selector<ReadonlyMap<string, Names>> {
  return cachedSelector(
    (state) => [state.events, state.agents],
    (state) => new Map(rows.map((row) => [row.notification_id, notificationNames(state, row)])),
  );
}

/** The text of one toast (its own selector: at most `MAX_TOASTS` are open). */
export function NotificationText({ row }: { readonly row: NotificationRow }) {
  const { app } = useAppEnv();
  const names = useStore((state) => notificationNames(state, row), app.store);
  return <NotificationMessage row={row} names={names} />;
}

/** The text of a notification with the names the list read for it. */
export function NotificationMessage({ row, names }: { readonly row: NotificationRow; readonly names: Names }) {
  const { t } = useI18n();
  const fallback = {
    org: t('notifications.unnamed.org'),
    wg: t('notifications.unnamed.wg'),
    task: t('notifications.unnamed.task'),
    subject: t('notifications.unnamed.subject'),
    agent: t('notifications.unnamed.agent'),
  };
  const message = notificationMessage(row, names, fallback);
  return <span data-testid="notifications.text">{t(message.key, message.params)}</span>;
}
