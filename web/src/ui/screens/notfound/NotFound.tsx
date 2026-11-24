// Not-found screen (06-web.md sections 2.4 and 12.6) in the frame of the error pages.
import { Link } from 'react-router-dom';
import { ERROR_LINK, ErrorLayout } from '../errors/ErrorLayout';
import { useI18n } from '../../i18n';

export function NotFound() {
  const { t } = useI18n();
  return (
    <ErrorLayout
      testId="notfound.page"
      prefix="notfound"
      title={t('notfound.title')}
      body={<p>{t('notfound.body')}</p>}
    >
      <Link className={ERROR_LINK} to="/" data-testid="notfound.home">
        {t('notfound.home')}
      </Link>
    </ErrorLayout>
  );
}
