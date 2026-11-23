// The sentence of a notification for the center and the toasts: kind + decrypted names (`core/notifications/text.ts`),
// neutral fallbacks while a name is not read yet; the names are pulled once per row set.
import { useEffect } from 'react';
import { pullNames } from '../../../core/notifications/actions';
import { notificationMessage, notificationNames } from '../../../core/notifications/text';
import type { NotificationRow } from '../../../core/store/slices/notifications';
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

export function NotificationText({ row }: { readonly row: NotificationRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const names = useStore((state) => notificationNames(state, row), app.store);
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
