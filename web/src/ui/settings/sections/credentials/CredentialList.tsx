// The stored credentials (06-web.md section 8.2): kind, version, last change and what may be shown of it (GitHub login,
// subscription e-mail, mailbox address and host – never a token, key or password), unverifiable records with their
// code, and "Delete" with a confirmation (`DELETE /v1/me/credentials/{kind}`; devices purge their caches).
import { useState } from 'react';
import { type CredentialKind, deleteCredential, type Summary } from '../../../../core/credentials/records';
import { formatEventTime } from '../../../../core/i18n';
import { requestErrorCode } from '../../../../core/requests/client';
import type { CredentialEntry } from '../../../../core/store/slices/credentials';
import { useI18n } from '../../../i18n';
import { Dialog } from '../../../screens/members/Dialog';
import { useNow } from '../../../countdown/useNow';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { BUTTON } from './DevicePicker';

function useDetail(): (summary: Summary) => string | null {
  const { t } = useI18n();
  return (summary) => {
    switch (summary.kind) {
      case 'github':
        return t('credentials.list.github', { login: summary.login });
      case 'claude_subscription':
        return summary.email === null ? null : t('credentials.list.email', { email: summary.email });
      case 'mailbox':
        return t('credentials.list.mailbox', { email: summary.email, host: summary.host });
      case 'anthropic_api_key':
        return null;
    }
  };
}

/** Origin ("long-lived token" / "from the Claude Code login", ADR-056 section 7) and expiry of a subscription token;
 *  an expired token is highlighted. */
function SubscriptionOrigin({
  summary,
  now,
}: {
  readonly summary: Extract<Summary, { kind: 'claude_subscription' }>;
  readonly now: number;
}) {
  const { t, locale } = useI18n();
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const expires = summary.expiresAt === null ? null : Date.parse(summary.expiresAt);
  const expired = expires !== null && expires <= now;
  return (
    <>
      <span data-testid="credentials.item.claude_subscription.origin" data-origin={summary.origin}>
        {t(`credentials.list.origin.${summary.origin}`)}
      </span>
      {expires !== null &&
        (expired ? (
          <span
            className="font-semibold text-red-800"
            role="alert"
            data-testid="credentials.item.claude_subscription.expiry"
            data-expired
          >
            {t('credentials.list.expired', { time: formatEventTime(expires, { locale, timeZone, now }) })}
          </span>
        ) : (
          <span
            className="text-sm text-gray-700"
            data-testid="credentials.item.claude_subscription.expiry"
            data-expired={false}
          >
            {t('credentials.list.expires', { time: formatEventTime(expires, { locale, timeZone, now }) })}
          </span>
        ))}
    </>
  );
}

export function CredentialList({
  entries,
  onChanged,
}: {
  readonly entries: readonly CredentialEntry[];
  readonly onChanged: () => void;
}) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const reason = useReason();
  const detail = useDetail();
  const [deleting, setDeleting] = useState<CredentialKind | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const now = useNow(false);
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const confirm = async (kind: CredentialKind) => {
    setBusy(true);
    setFailure(null);
    try {
      await deleteCredential(app.tasks, kind);
      setDeleting(null);
      onChanged();
    } catch (error) {
      setFailure(requestErrorCode(error));
    } finally {
      setBusy(false);
    }
  };
  if (entries.length === 0) return <p data-testid="credentials.empty">{t('credentials.list.empty')}</p>;
  return (
    <>
      <ul className="flex flex-col divide-y divide-gray-200" data-testid="credentials.list">
        {entries.map((entry) => {
          const shown = entry.summary === null ? null : detail(entry.summary);
          return (
            <li
              key={entry.kind}
              className="flex flex-wrap items-center gap-3 py-2"
              data-testid={`credentials.item.${entry.kind}`}
              data-status={entry.status}
            >
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="font-medium">{t(`credentials.kind.${entry.kind}`)}</span>
                {shown !== null && <span data-testid={`credentials.item.${entry.kind}.detail`}>{shown}</span>}
                {entry.summary?.kind === 'claude_subscription' && (
                  <SubscriptionOrigin summary={entry.summary} now={now} />
                )}
                {entry.status === 'unverifiable' && (
                  <span
                    role="alert"
                    className="text-sm text-red-800"
                    data-testid={`credentials.item.${entry.kind}.code`}
                  >
                    {t('credentials.list.unverifiable', { code: String(entry.code) })}
                  </span>
                )}
                <span className="text-sm text-gray-600" data-testid={`credentials.item.${entry.kind}.version`}>
                  {t('credentials.list.version', {
                    version: entry.version,
                    time: formatEventTime(Date.parse(entry.updatedAt), { locale, timeZone, now }),
                  })}
                </span>
              </div>
              <button
                type="button"
                className={BUTTON}
                onClick={() => setDeleting(entry.kind)}
                data-testid={`credentials.item.${entry.kind}.delete`}
              >
                {t('credentials.list.delete')}
              </button>
            </li>
          );
        })}
      </ul>
      {deleting !== null && (
        <Dialog
          title={t('credentials.delete.title', { kind: t(`credentials.kind.${deleting}`) })}
          testId="credentials.delete.dialog"
          busy={busy}
          onClose={() => setDeleting(null)}
        >
          <p>{t('credentials.delete.body')}</p>
          {failure !== null && (
            <p role="alert" data-testid="credentials.delete.failed" data-code={failure}>
              {t('credentials.delete.failed', { reason: reason(failure) })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="min-h-11 rounded bg-red-700 px-4 text-white"
              onClick={() => void confirm(deleting)}
              disabled={busy}
              data-testid="credentials.delete.confirm"
            >
              {t('credentials.list.delete')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => setDeleting(null)}
              disabled={busy}
              data-testid="credentials.delete.cancel"
            >
              {t('credentials.cancel')}
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}
