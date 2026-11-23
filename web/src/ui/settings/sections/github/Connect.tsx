// "Connect GitHub" with both ways (06-web.md section 8.1, KOR-25, ADR-059): the device flow of the GitHub App and
// "Use an existing token", each through `ConnectFlow`. One component for the settings section, the waiting card in a
// task's chat and the composer's hint, so every place offers the same flow (also on a phone: buttons of 44 px, the
// flow stacks). After a connection the `github` slice is loaded again (the section passes its own reload instead).
import { useCallback, useState } from 'react';
import { loadGithub } from '../../../../core/github/connection';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { type ConnectMethod, ConnectFlow } from './ConnectFlow';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-4';

export function GithubConnect({ onConnected }: { readonly onConnected?: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [method, setMethod] = useState<ConnectMethod | null>(null);
  const connected = useCallback(() => {
    setMethod(null);
    if (onConnected !== undefined) onConnected();
    else loadGithub(app.tasks).catch(() => undefined);
  }, [app, onConnected]);
  if (method !== null) return <ConnectFlow method={method} onConnected={connected} onClose={() => setMethod(null)} />;
  return (
    <div className="flex flex-wrap gap-2" data-testid="github.ways">
      <button
        type="button"
        className="min-h-11 rounded bg-blue-700 px-4 text-white"
        onClick={() => setMethod('device_flow')}
        data-testid="github.connect"
      >
        {t('github.connect')}
      </button>
      <button type="button" className={BUTTON} onClick={() => setMethod('token')} data-testid="github.token">
        {t('github.token.open')}
      </button>
    </div>
  );
}
