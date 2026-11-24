// "Connect GitHub" with both ways (06-web.md section 8.1, KOR-25, ADR-065): the sign-in through GitHub (the device flow
// of the OAuth App, else of the GitHub App; KOR-65) and "Use an existing token", each through `ConnectFlow`. The
// sign-in is offered only while a device runs one (`githubWays`; unknown or failed: offered, the flow then says what
// is missing); otherwise the section says so and offers the token alone – never a button that cannot work (E-05). One component for the settings section, the waiting card in a
// task's chat and the composer's hint, so every place offers the same flow (also on a phone: buttons of 44 px, the
// flow stacks). After a connection the `github` slice is loaded again (the section passes its own reload instead).
import { useCallback, useEffect, useState } from 'react';
import { githubWays, loadGithub } from '../../../../core/github/connection';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { type ConnectMethod, ConnectFlow } from './ConnectFlow';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-4';

export function GithubConnect({
  onConnected,
  app,
}: {
  readonly onConnected?: () => void;
  /** Sign in through the OAuth App only (the step "connect" of a held operation, KOR-65). */
  readonly app?: 'oauth_app';
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [method, setMethod] = useState<ConnectMethod | null>(null);
  const [signIn, setSignIn] = useState<boolean | null>(null);
  useEffect(() => {
    githubWays(app.tasks).then(setSignIn, () => setSignIn(null));
  }, [app]);
  const connected = useCallback(() => {
    setMethod(null);
    if (onConnected !== undefined) onConnected();
    else loadGithub(app.tasks).catch(() => undefined);
  }, [app, onConnected]);
  if (method !== null)
    return <ConnectFlow method={method} app={app} onConnected={connected} onClose={() => setMethod(null)} />;
  return (
    <div className="flex flex-wrap gap-2" data-testid="github.ways">
      {signIn === false ? (
        <p className="w-full text-sm" data-testid="github.ways.token-only">
          {t('github.ways.token-only')}
        </p>
      ) : (
        <button
          type="button"
          className="min-h-11 rounded bg-blue-700 px-4 text-white"
          onClick={() => setMethod('device_flow')}
          data-testid="github.connect"
        >
          {t('github.connect')}
        </button>
      )}
      <button type="button" className={BUTTON} onClick={() => setMethod('token')} data-testid="github.token">
        {t('github.token.open')}
      </button>
    </div>
  );
}
