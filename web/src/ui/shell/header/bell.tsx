// The notification bell (06-web.md section 6.2): a link to the center with the unread badge (`unread_notifications`
// of `GET /v1/me`, updated by live `notification` frames), and the toasts of live notifications – a polite live region
// (06-web.md section 12.3), each toast closes by itself after `TOAST_MS` (paused while hovered or focused, WCAG 2.2.1)
// or with its button; a click on the text marks
// it read and navigates like the center.
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { dismissToast, type NotificationRow, selectUnread } from '../../../core/store/slices/notifications';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useOpen } from '../../screens/notifications/NotificationsScreen';
import { NotificationText, useNames } from '../../screens/notifications/text';
import { useAppEnv } from '../context';

export const TOAST_MS = 6_000;

function Toast({ row }: { readonly row: NotificationRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const open = useOpen();
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const timer = setTimeout(() => dismissToast(app.store, row.notification_id), TOAST_MS);
    return () => clearTimeout(timer);
  }, [app, row.notification_id, paused]);
  return (
    <li
      className="flex items-center gap-3 rounded bg-gray-900 px-4 py-2 text-white shadow"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      data-testid="bell.toast"
      data-paused={paused}
    >
      <button
        type="button"
        className="min-h-11 text-left underline-offset-2 hover:underline"
        onClick={() => {
          dismissToast(app.store, row.notification_id);
          void open(row);
        }}
        data-testid="bell.toast.open"
      >
        <NotificationText row={row} />
      </button>
      <button
        type="button"
        className="min-h-11 min-w-11"
        onClick={() => dismissToast(app.store, row.notification_id)}
        aria-label={t('notifications.toast.close')}
        data-testid="bell.toast.close"
      >
        ×
      </button>
    </li>
  );
}

export function Bell() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const unread = useStore(selectUnread, app.store);
  const ids = useStore((state) => state.notifications.toasts, app.store);
  const all = useStore((state) => state.notifications.rows, app.store);
  const toasts = useMemo(() => ids.map((id) => all[id]!), [ids, all]);
  useNames(toasts);
  // the unread count of `GET /v1/me` again: `notification` frames may have been lost (KOR-11)
  useLiveReload(app.store, () => void app.loadMe().catch(() => null));
  return (
    <>
      <Link
        to="/notifications"
        className="relative flex min-h-11 min-w-11 items-center justify-center"
        aria-label={unread > 0 ? t('notifications.bell.unread', { count: unread }) : t('notifications.bell')}
        data-testid="bell.link"
      >
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6 fill-none stroke-current stroke-2">
          <path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8" />
          <path d="M10 20a2 2 0 0 0 4 0" />
        </svg>
        {unread > 0 && (
          <span
            className="absolute right-0 top-0 min-w-5 rounded-full bg-red-700 px-1 text-center text-xs text-white"
            data-testid="bell.badge"
          >
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </Link>
      {/* Below `md` the toasts are the header's last row, in the flow: fixed at the bottom they covered a page's last
          actions (the chat composer's Send), fixed at the top the controls of a wrapped header. While empty the list
          is taken out of the flow, so it adds no row (and no row gap) but stays in the tree as the live region. */}
      <ul
        aria-live="polite"
        className="order-last flex basis-full flex-col gap-2 empty:absolute md:fixed md:bottom-4 md:left-4 md:z-30 md:basis-auto"
        data-testid="bell.toasts"
      >
        {toasts.map((row) => (
          <Toast key={row.notification_id} row={row} />
        ))}
      </ul>
    </>
  );
}

export const headerItem = { id: 'bell', order: 20, Component: Bell };
