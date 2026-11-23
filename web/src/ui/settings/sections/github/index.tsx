// Settings section "GitHub" (06-web.md section 8.1, ADR-015, ADR-059): the connection state from the verified,
// decrypted `github` credential (login, how it was made, its scopes – never the token), "Connect GitHub" and "Use an
// existing token" (device request `github.connect`, `Connect.tsx`/`ConnectFlow.tsx`) and
// "Disconnect" (confirmation, `DELETE /v1/me/credentials/github`, then a link to the GitHub settings page where the
// user revokes the app authorization).
import { useCallback, useEffect, useState } from 'react';
import { disconnectGithub, GITHUB_SETTINGS_URL, loadGithub } from '../../../../core/github/connection';
import { requestErrorCode } from '../../../../core/requests/client';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { Dialog } from '../../../screens/members/Dialog';
import { useAppEnv } from '../../../shell/context';
import { GithubConnect } from './Connect';
import { useReason } from './reason';

const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4';

export function GithubSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const connection = useStore((state) => state.github, app.store);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [disconnectError, setDisconnectError] = useState<string | null>(null);
  const [disconnected, setDisconnected] = useState(false);
  const load = useCallback(() => {
    loadGithub(app.tasks).catch((error: unknown) => setLoadError(requestErrorCode(error)));
  }, [app]);
  useEffect(load, [load]);
  const reload = () => {
    setLoadError(null);
    load();
  };
  const connected = useCallback(() => {
    setDisconnected(false);
    load();
  }, [load]);
  const disconnect = async () => {
    setBusy(true);
    setDisconnectError(null);
    try {
      await disconnectGithub(app.tasks);
      setDisconnected(true);
      setConfirming(false);
    } catch (error) {
      setDisconnectError(requestErrorCode(error));
    } finally {
      setBusy(false);
    }
  };
  if (loadError !== null)
    return (
      <div className="flex flex-col gap-2">
        <p role="alert" data-testid="github.load-failed">
          {t('github.load-failed', { reason: reason(loadError) })}
        </p>
        <button type="button" className={BUTTON} onClick={reload} data-testid="github.reload">
          {t('github.retry')}
        </button>
      </div>
    );
  if (connection.status === 'unknown') return <p data-testid="github.loading">{t('github.loading')}</p>;
  const canConnect = connection.status !== 'connected';
  return (
    <div className="flex flex-col gap-3" data-testid="github.state" data-status={connection.status}>
      {connection.status === 'connected' && (
        <div className="flex flex-col gap-1">
          <p data-testid="github.connected">{t('github.connected', { login: connection.login })}</p>
          <p className="text-sm" data-testid="github.source" data-source={connection.source}>
            {t('github.source', { source: t(`github.source.${connection.source}`) })}
          </p>
          <p className="text-sm" data-testid="github.scopes">
            {connection.scopes.length > 0
              ? t('github.scopes', { scopes: connection.scopes.join(', ') })
              : t(`github.scopes.none.${connection.source}`)}
          </p>
          {connection.source === 'token' && connection.scopes.length > 0 && (
            <p className="text-sm text-gray-700" data-testid="github.scopes.classic">
              {t('github.scopes.classic')}
            </p>
          )}
        </div>
      )}
      {connection.status === 'disconnected' && <p data-testid="github.disconnected">{t('github.disconnected')}</p>}
      {connection.status === 'unverifiable' && (
        <p role="alert" data-testid="github.unverifiable">
          {t('github.unverifiable', { code: connection.code })}
        </p>
      )}
      {disconnected && (
        <p role="status" data-testid="github.disconnected-note">
          {t('github.disconnected-note')}{' '}
          <a
            className="underline"
            href={GITHUB_SETTINGS_URL}
            target="_blank"
            rel="noopener noreferrer"
            data-testid="github.settings-link"
          >
            {t('github.github-settings')}
          </a>
        </p>
      )}
      {canConnect && <GithubConnect onConnected={connected} />}
      {connection.status !== 'disconnected' && (
        <button type="button" className={BUTTON} onClick={() => setConfirming(true)} data-testid="github.disconnect">
          {t('github.disconnect')}
        </button>
      )}
      {confirming && (
        <Dialog
          title={t('github.disconnect.title')}
          testId="github.disconnect.dialog"
          busy={busy}
          onClose={() => setConfirming(false)}
        >
          <p>{t('github.disconnect.body')}</p>
          {disconnectError !== null && (
            <p role="alert" data-testid="github.disconnect.failed">
              {t('github.disconnect.failed', { reason: reason(disconnectError) })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="min-h-11 rounded bg-red-700 px-4 text-white"
              onClick={() => void disconnect()}
              disabled={busy}
              data-testid="github.disconnect.confirm"
            >
              {t('github.disconnect')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => setConfirming(false)}
              disabled={busy}
              data-testid="github.disconnect.cancel"
            >
              {t('github.cancel')}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export const section: SettingsSection = { id: 'github', order: 50, titleKey: 'github.title', Component: GithubSection };
