// Blocking error page (06-web.md sections 10.4 and 12.6) shown instead of the app: title, explanation, next step and
// the error code from the error catalog, with "Reload" (e.g. after the hub was unavailable, `503`).
import { ERROR_BUTTON, ErrorLayout } from '../screens/errors/ErrorLayout';
import { useI18n } from '../i18n';

export interface ErrorPageProps {
  readonly code: string;
  readonly onReload?: () => void;
}

export function ErrorPage({ code, onReload = () => window.location.reload() }: ErrorPageProps) {
  const { t, error } = useI18n();
  const message = error(code);
  return (
    <ErrorLayout
      testId="error.page"
      prefix="error"
      size="page"
      title={message.title}
      body={<p>{message.body}</p>}
      action={message.action}
      code={code}
    >
      <button type="button" className={ERROR_BUTTON} onClick={onReload} data-testid="error.reload">
        {t('app.error.reload')}
      </button>
    </ErrorLayout>
  );
}
