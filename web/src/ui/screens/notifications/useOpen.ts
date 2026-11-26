// Opening a notification (06-web.md section 6.2), shared by the notification center and the header bell; a module of
// its own so the bell in the start bundle does not pull the whole screen in (06-web.md section 12.2).
import { useNavigate } from 'react-router-dom';
import { meOrgs } from '../../../core/account/session';
import { readNotification } from '../../../core/notifications/actions';
import { notificationTarget } from '../../../core/notifications/text';
import type { NotificationRow } from '../../../core/store/slices/notifications';
import { useAppEnv } from '../../shell/context';

/** Marks a row read (best effort: navigation never waits for a failure) and opens its target. */
export function useOpen(): (row: NotificationRow) => Promise<void> {
  const { app } = useAppEnv();
  const navigate = useNavigate();
  return async (row) => {
    if (row.read_at === null) await readNotification(app.tasks, row.notification_id).catch(() => undefined);
    const member = meOrgs(app.store.getState().me.profile).some((org) => org.org_id === row.org_id);
    navigate(notificationTarget(row, member));
  };
}
