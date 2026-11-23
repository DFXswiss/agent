// Settings section "Credentials" (06-web.md section 8.2, ADR-010, ADR-011): the stored credentials (list with version
// and "Delete"), the Anthropic API key, the Claude subscription (relay or mailbox automation, `LoginFlow.tsx`) and the
// mailbox of the automation. All records are encrypted and opened in the browser under the user's `CK`
// (`core/credentials/records.ts`); only their non-secret summary is shown. GitHub has its own section (WEB-09).
import { useCallback, useEffect, useState } from 'react';
import { loadCredentials } from '../../../../core/credentials/records';
import { requestErrorCode } from '../../../../core/requests/client';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { ApiKey } from './ApiKey';
import { CredentialList } from './CredentialList';
import { BUTTON, PRIMARY } from './DevicePicker';
import { LoginFlow } from './LoginFlow';
import { Mailbox } from './Mailbox';

export function CredentialsSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const entries = useStore((state) => state.credentials.entries, app.store);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [logging, setLogging] = useState(false);
  const [connected, setConnected] = useState<{ readonly email: string | null } | null>(null);
  const load = useCallback(() => {
    loadCredentials(app.tasks).catch((error: unknown) => setLoadError(requestErrorCode(error)));
  }, [app]);
  useEffect(load, [load]);
  const reload = () => {
    setLoadError(null);
    load();
  };
  const done = useCallback(
    (email: string | null) => {
      setLogging(false);
      setConnected({ email });
      load();
    },
    [load],
  );
  if (loadError !== null)
    return (
      <div className="flex flex-col gap-2">
        <p role="alert" data-testid="credentials.load-failed" data-code={loadError}>
          {t('credentials.load-failed', { reason: reason(loadError) })}
        </p>
        <button type="button" className={BUTTON} onClick={reload} data-testid="credentials.reload">
          {t('credentials.retry')}
        </button>
      </div>
    );
  if (entries === null) return <p data-testid="credentials.loading">{t('credentials.loading')}</p>;
  const stored = (kind: string) => entries.some((entry) => entry.kind === kind && entry.status === 'verified');
  return (
    <div className="flex flex-col gap-6" data-testid="credentials.section">
      <CredentialList entries={entries} onChanged={load} />
      <ApiKey stored={stored('anthropic_api_key')} onSaved={load} />
      <div className="flex flex-col gap-2" data-testid="claude">
        <h3 className="font-semibold">{t('credentials.claude.title')}</h3>
        <p className="text-sm text-gray-600">{t('credentials.claude.intro')}</p>
        {connected !== null && (
          <p role="status" data-testid="claude.connected">
            {connected.email === null
              ? t('credentials.claude.connected')
              : t('credentials.claude.connected-as', { email: connected.email })}
          </p>
        )}
        {!logging && (
          <button
            type="button"
            className={PRIMARY}
            onClick={() => {
              setConnected(null);
              setLogging(true);
            }}
            data-testid="claude.connect"
          >
            {stored('claude_subscription') ? t('credentials.claude.reconnect') : t('credentials.claude.connect')}
          </button>
        )}
        {logging && <LoginFlow automatic={stored('mailbox')} onDone={done} onClose={() => setLogging(false)} />}
      </div>
      <Mailbox stored={stored('mailbox')} onSaved={load} />
    </div>
  );
}

export const section: SettingsSection = {
  id: 'credentials',
  order: 62,
  titleKey: 'credentials.title',
  Component: CredentialsSection,
};
