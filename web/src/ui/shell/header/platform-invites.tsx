// Header link to the platform invitations (06-web.md section 7.4): rendered only for users whose verified platform log
// grants the role `inviter` (06-web.md section 1.2; a navigation entry would show it to everybody).
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { useInviter } from '../../screens/platform-invites/PlatformInvitesScreen';

export function PlatformInvitesLink() {
  const { t } = useI18n();
  if (useInviter() !== true) return null;
  return (
    <Link className="flex min-h-11 items-center underline" to="/platform/invites" data-testid="shell.platform-invites">
      {t('platform-invites.link')}
    </Link>
  );
}

export const headerItem = { id: 'platform-invites', order: 80, Component: PlatformInvitesLink };
