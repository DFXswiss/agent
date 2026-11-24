// Confirmation after joining an organization from an invitation (KOR-36): a status row across the header, "You are now
// a member of <org>", shown while the user works in that org until closed. The invitation screen sets it
// (`noteJoined`) right before it opens the org.
import { dismissJoined } from '../../../core/store/slices/joined';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../context';

export function JoinedNotice() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const notice = useStore((state) => state.joined.notice, app.store);
  const active = useStore((state) => state.session.activeOrg, app.store);
  if (notice === null || notice.org !== active) return null;
  return (
    <div
      role="status"
      className="order-last flex basis-full items-center gap-3 rounded bg-green-50 px-3 py-2 text-green-900"
      data-testid="shell.joined"
      data-org={notice.org}
    >
      <span className="mr-auto">{t('invite-org.joined', { org: notice.orgName || t('invite-org.org-unnamed') })}</span>
      <button
        type="button"
        className="min-h-11 min-w-11 underline"
        onClick={() => dismissJoined(app.store)}
        aria-label={t('invite-org.joined.close')}
        data-testid="shell.joined.close"
      >
        ×
      </button>
    </div>
  );
}

export const headerItem = { id: 'joined', order: 5, Component: JoinedNotice };
