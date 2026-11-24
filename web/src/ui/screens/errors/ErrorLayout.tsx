// The common frame of the error pages (06-web.md section 12.6): a level-1 heading announced as an alert, the explanation, the
// next step, optionally the error code for support and the actions. Used full-page (outside the shell: blocking
// configuration errors, unsupported browser) and inside the shell in place of a screen.
import type { ReactNode } from 'react';
import { useI18n } from '../../i18n';

export interface ErrorLayoutProps {
  /** Test ID of the page; its parts are `<prefix>.title`, `.body`, `.action` and `.code`. */
  readonly testId: string;
  readonly prefix: string;
  /** `page`: the whole viewport (no shell around it); `screen`: in place of a screen. */
  readonly size?: 'page' | 'screen';
  readonly title: string;
  readonly body: ReactNode;
  readonly action?: string;
  /** Shown as "Error code: …" for support. */
  readonly code?: string;
  readonly children?: ReactNode;
}

export function ErrorLayout({
  testId,
  prefix,
  size = 'screen',
  title,
  body,
  action,
  code,
  children,
}: ErrorLayoutProps) {
  const { t } = useI18n();
  const frame = size === 'page' ? 'min-h-screen justify-center' : '';
  return (
    <main className={`mx-auto flex w-full max-w-xl flex-col gap-4 p-6 ${frame}`} data-testid={testId}>
      {/* the alert wraps the heading, so the title keeps its level-1 heading role */}
      <div role="alert">
        <h1 className="text-2xl font-semibold" data-testid={`${prefix}.title`}>
          {title}
        </h1>
      </div>
      <div data-testid={`${prefix}.body`}>{body}</div>
      {action !== undefined && <p data-testid={`${prefix}.action`}>{action}</p>}
      {code !== undefined && (
        <p className="break-all font-mono text-sm text-gray-700" data-testid={`${prefix}.code`}>
          {t('app.error.code', { code })}
        </p>
      )}
      {children !== undefined && <div className="flex flex-wrap gap-3">{children}</div>}
    </main>
  );
}

export const ERROR_BUTTON = 'min-h-11 rounded border border-gray-400 px-4';
export const ERROR_LINK = 'inline-flex min-h-11 items-center underline';
