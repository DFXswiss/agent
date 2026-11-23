// Page footer on every page (06-web.md section 10.5): the link to the security page, also for anonymous visitors.
import { Link } from 'react-router-dom';
import { useI18n } from '../i18n';

export function Footer() {
  const { t } = useI18n();
  return (
    <footer className="border-t border-gray-200 px-4 text-sm" data-testid="shell.footer">
      <Link className="inline-flex min-h-11 items-center underline" to="/security" data-testid="shell.footer.security">
        {t('security.nav')}
      </Link>
    </footer>
  );
}
