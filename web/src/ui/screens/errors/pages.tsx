// The error pages of 06-web.md section 12.6 shown in place of a screen: not available (for "not found" and
// "forbidden" alike, so a page never reveals whether something exists), session expired (sign in again with the
// passkey, then the screen loads again), hub unavailable and any other load error (catalog text, error code, "Try
// again"). `LoadError` picks the page for an error code.
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { useRenewal } from '../../shell/RenewalNotice';
import { ERROR_BUTTON, ERROR_LINK, ErrorLayout } from './ErrorLayout';

/** Codes that show the neutral "not available" page. */
export const HIDDEN_CODES: readonly string[] = ['not_found', 'forbidden'];

export function NotAvailable({ testId }: { readonly testId: string }) {
  const { t } = useI18n();
  return (
    <ErrorLayout
      testId={testId}
      prefix="not-available"
      title={t('error-pages.not-available.title')}
      body={t('error-pages.not-available.body')}
    >
      <Link className={ERROR_LINK} to="/" data-testid="not-available.home">
        {t('error-pages.home')}
      </Link>
    </ErrorLayout>
  );
}

export function SessionExpired({ testId, onRenewed }: { readonly testId: string; readonly onRenewed: () => void }) {
  const { t, error } = useI18n();
  const { working, renew, message } = useRenewal(onRenewed);
  const text = error('session_expired');
  return (
    <ErrorLayout testId={testId} prefix="session-expired" title={text.title} body={text.body} action={text.action}>
      <button
        type="button"
        className={ERROR_BUTTON}
        onClick={() => void renew()}
        disabled={working}
        data-testid="session-expired.renew"
      >
        {working ? t('shell.renew.working') : t('shell.renew.action')}
      </button>
      {message !== null && (
        <p className="w-full" role="alert" data-testid="session-expired.message">
          {message}
        </p>
      )}
    </ErrorLayout>
  );
}

export interface LoadErrorProps {
  readonly code: string;
  readonly testId: string;
  /** Loads the screen again ("Try again", and after signing in again). */
  readonly retry: () => void;
}

export function LoadError({ code, testId, retry }: LoadErrorProps) {
  const { t, error } = useI18n();
  if (HIDDEN_CODES.includes(code)) return <NotAvailable testId={testId} />;
  if (code === 'session_expired') return <SessionExpired testId={testId} onRenewed={retry} />;
  const text = error(code);
  return (
    <ErrorLayout
      testId={testId}
      prefix="load-error"
      title={text.title}
      body={text.body}
      action={text.action}
      code={code}
    >
      <button type="button" className={ERROR_BUTTON} onClick={retry} data-testid="load-error.retry">
        {t('error-pages.retry')}
      </button>
    </ErrorLayout>
  );
}
