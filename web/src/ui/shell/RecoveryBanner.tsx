// Persistent reminder after the recovery code was skipped (06-web.md section 3.2), until it is confirmed in /settings.
import { Link } from 'react-router-dom';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from './context';

export function RecoveryBanner() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const show = useStore((state) => state.session.recoveryUnconfirmed && state.session.phase === 'unlocked', app.store);
  if (!show) return null;
  return (
    <p
      className="flex flex-wrap items-center gap-3 bg-red-50 px-4 py-2 text-red-900"
      role="status"
      data-testid="shell.recovery-banner"
    >
      <span>{t('shell.recovery.banner')}</span>
      <Link className="underline" to="/settings" data-testid="shell.recovery-banner.settings">
        {t('shell.recovery.banner.action')}
      </Link>
    </p>
  );
}
