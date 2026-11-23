// The notification center (06-web.md section 6.2): the user's notifications newest first with client-side, content-free
// texts and the time; unread rows are marked; a click marks the row read and navigates to its target; "Mark all read";
// "Load more" for older rows. New rows of the active org arrive live.
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { parseTime } from '../../../core/encoding/time';
import { loadNotifications, readAll, readNotification } from '../../../core/notifications/actions';
import { notificationTarget } from '../../../core/notifications/text';
import { type NotificationRow, sortedRows } from '../../../core/store/slices/notifications';
import { sendErrorCode } from '../../../core/tasks/send';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { Time } from '../../whiteboard/Time';
import { NotificationText, useNames } from './text';

function timeOf(row: NotificationRow): number {
  try {
    return parseTime(row.created_at);
  } catch {
    return 0;
  }
}

/** Marks a row read (best effort: navigation never waits for a failure) and opens its target. */
export function useOpen(): (row: NotificationRow) => Promise<void> {
  const { app } = useAppEnv();
  const navigate = useNavigate();
  return async (row) => {
    if (row.read_at === null) await readNotification(app.tasks, row.notification_id).catch(() => undefined);
    navigate(notificationTarget(row));
  };
}

export function NotificationsScreen() {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const all = useStore((state) => state.notifications.rows, app.store);
  const list = useStore((state) => state.notifications.list, app.store);
  const rows = useMemo(() => sortedRows(all), [all]);
  const open = useOpen();
  const [code, setCode] = useState<string | null>(null);
  useNames(rows);
  useEffect(() => {
    void loadNotifications(app.tasks);
  }, [app]);
  useLiveReload(app.store, () => void loadNotifications(app.tasks));
  const markAll = async () => {
    setCode(null);
    try {
      await readAll(app.tasks);
    } catch (failure) {
      setCode(sendErrorCode(failure));
    }
  };
  const unread = rows.some((row) => row.read_at === null);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="notifications.screen">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{t('notifications.title')}</h1>
        {unread && (
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-3"
            onClick={() => void markAll()}
            data-testid="notifications.mark-all"
          >
            {t('notifications.mark-all')}
          </button>
        )}
      </header>
      {code !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="notifications.mark-all.error">
          {error(code).title}
        </p>
      )}
      {list.status === 'error' && (
        <div role="alert" className="flex items-center gap-2 text-red-800" data-testid="notifications.error">
          <span>{error(list.code!).title}</span>
          <button type="button" className="min-h-11 underline" onClick={() => void loadNotifications(app.tasks)}>
            {t('notifications.reload')}
          </button>
        </div>
      )}
      {rows.length === 0 && list.status === 'ready' && (
        <p className="text-sm text-gray-600" data-testid="notifications.empty">
          {t('notifications.empty')}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-gray-200" data-testid="notifications.list">
        {rows.map((row) => (
          <li key={row.notification_id}>
            <button
              type="button"
              className={`flex min-h-11 w-full flex-col items-start gap-1 px-2 py-2 text-left hover:bg-gray-50 ${row.read_at === null ? 'font-medium' : 'text-gray-700'}`}
              onClick={() => void open(row)}
              data-testid="notifications.item"
              data-kind={row.kind}
              data-unread={row.read_at === null}
            >
              <span className="flex items-center gap-2">
                {row.read_at === null && (
                  <span className="h-2 w-2 rounded-full bg-blue-700" aria-label={t('notifications.unread')} />
                )}
                <NotificationText row={row} />
              </span>
              <Time at={timeOf(row)} testId="notifications.item.time" />
            </button>
          </li>
        ))}
      </ul>
      {list.nextBefore !== null && (
        <button
          type="button"
          className="min-h-11 self-center underline"
          onClick={() => void loadNotifications(app.tasks, true)}
          data-testid="notifications.more"
        >
          {t('notifications.more')}
        </button>
      )}
    </main>
  );
}
