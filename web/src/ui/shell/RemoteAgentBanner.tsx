// The banner of a main orchestrator on another computer (ADR-062 section 4.4): while the user's main orchestrator
// (the newest confirmed or active agent) was paired from another computer, every signed-in page shows "Your main
// orchestrator runs on another computer (<host>), connected <time>" with "Disconnect". It comes from the agent list
// (`GET /v1/me/agents`, loaded once the identity is unlocked), never from a notification's read state, so an agent
// that marks its notification read cannot hide it. A new notification of a remote pairing (or of a revoked agent)
// loads the list again, so the banner also appears in a client that was open while the pairing was confirmed
// elsewhere. "Disconnect" revokes the agent at once and reloads the list.
import { useEffect, useState } from 'react';
import { agentChanges, loadAgents, revokeAgent } from '../../core/agents/list';
import { formatEventTime } from '../../core/i18n';
import { requestErrorCode } from '../../core/requests/client';
import { remoteOrchestrator } from '../../core/store/slices/agents';
import { useNow } from '../countdown/useNow';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from './context';

export function RemoteAgentBanner() {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const remote = useStore((state) => remoteOrchestrator(state.agents.entries), app.store);
  const [busy, setBusy] = useState(false);
  const now = useNow(false);
  const [failure, setFailure] = useState<string | null>(null);
  const changes = useStore((state) => agentChanges(state.notifications.rows), app.store);
  useEffect(() => void loadAgents(app.tasks), [app, changes]);
  if (remote === null) return null;
  const disconnect = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await revokeAgent(app.tasks, remote.agentId);
    } catch (caught) {
      setFailure(requestErrorCode(caught));
    } finally {
      setBusy(false);
    }
  };
  const time = formatEventTime(Date.parse(remote.confirmedAt), {
    locale,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    now,
  });
  return (
    <div
      className="flex flex-wrap items-center gap-3 border-b-2 border-red-700 bg-red-50 px-4 py-2 text-red-900"
      role="alert"
      data-testid="shell.remote-agent"
      data-agent={remote.agentId}
    >
      <span className="flex-1" data-testid="shell.remote-agent.text">
        {t('shell.remote-agent', { host: remote.label?.host ?? t('shell.remote-agent.unknown-host'), time })}
      </span>
      <button
        type="button"
        className="min-h-11 rounded bg-red-700 px-4 text-white disabled:opacity-60"
        onClick={() => void disconnect()}
        disabled={busy}
        data-testid="shell.remote-agent.disconnect"
      >
        {t('shell.remote-agent.disconnect')}
      </button>
      {failure !== null && (
        <span className="w-full" data-testid="shell.remote-agent.failed">
          {error(failure).title}
        </span>
      )}
    </div>
  );
}
