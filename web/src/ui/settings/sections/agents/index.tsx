// Settings section "Connected agents" (06-web.md section 8.3, 11-agent-access.md section 5.3): the user's main
// orchestrator (concept idea 34: one at a time, the newest confirmed or active agent) and below it the earlier agents
// from `GET /v1/me/agents`, each with name and computer (opened from the sealed label; "Unknown agent" when it does
// not open), the agent it replaced (when that one's name is known), created, last used and state; "Disconnect" and
// "Disconnect all agents" after a dialog that says the agent loses all access at once and keeps what it has already
// read, that it is removed from every organization with its keys replaced, and – for an agent that held a copy of the
// user's identity (`held_identity`, paired before ADR-082) – that this copy keeps opening what is encrypted for the
// user. The log clean-up runs right after the revocation (`revokeAgent`: the log job of every org, the job `agents`
// removes the agent with its rotations, the credential key is rotated); the dialog stays open meanwhile.
// `?agent=connected|failed` (the agent's redirect after a pairing) is only a hint above the list; the state shown is
// always the hub's. Where the agent runs: "Runs on another computer" (ADR-067) or, for an agent with its own key, on
// its computer (stage b adds the orchestrator session).
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { loadAgents } from '../../../../core/agents/list';
import { formatEventTime } from '../../../../core/i18n';
import { requestErrorCode } from '../../../../core/requests/client';
import { type AgentEntry, agentName, LIVE_STATES, mainOrchestrator } from '../../../../core/store/slices/agents';
import { useNow } from '../../../countdown/useNow';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import type { SettingsSection } from '../../../registries/settings-sections';
import { Dialog } from '../../../screens/members/Dialog';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { SecurityActivity } from './SecurityActivity';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-4';
const DANGER = 'min-h-11 rounded bg-red-700 px-4 text-white disabled:opacity-60';

function useTime(): (at: string) => string {
  const { locale } = useI18n();
  const now = useNow(false);
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (at) => formatEventTime(Date.parse(at), { locale, timeZone, now });
}

function Item({ entry, onDisconnect }: { readonly entry: AgentEntry; readonly onDisconnect: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const time = useTime();
  const replaced = useStore((state) => (entry.replaces === null ? null : agentName(state, entry.replaces)), app.store);
  return (
    <li
      className="flex flex-wrap items-center gap-3 py-2"
      data-testid="agents.item"
      data-agent={entry.agentId}
      data-state={entry.state}
    >
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="break-words font-medium" data-testid="agents.item.name">
          {entry.label === null ? t('agents.unknown') : entry.label.name}
        </span>
        {entry.label !== null && (
          <span className="break-words" data-testid="agents.item.host">
            {t('agents.host', { host: entry.label.host })}
          </span>
        )}
        {entry.remote && (
          <span className="text-sm font-semibold text-red-800" data-testid="agents.item.remote">
            {t('agents.remote')}
          </span>
        )}
        {!entry.remote && entry.ownIdentity && (
          <span className="text-sm text-gray-700" data-testid="agents.item.runs">
            {t('agents.runs.ownComputer')}
          </span>
        )}
        {entry.heldIdentity && (
          <span className="text-sm text-gray-700" data-testid="agents.item.held">
            {t('agents.held')}
          </span>
        )}
        {replaced !== null && (
          <span className="text-sm text-gray-700" data-testid="agents.item.replaces">
            {t('agents.replaces', { name: replaced })}
          </span>
        )}
        <span className="text-sm text-gray-600" data-testid="agents.item.created">
          {t('agents.created', { time: time(entry.createdAt) })}
        </span>
        <span className="text-sm text-gray-600" data-testid="agents.item.last-used">
          {entry.lastUsedAt === null ? t('agents.never-used') : t('agents.last-used', { time: time(entry.lastUsedAt) })}
        </span>
        <span className="text-sm" data-testid="agents.item.state">
          {t(`agents.state.${entry.state}`)}
        </span>
      </div>
      {LIVE_STATES.includes(entry.state) && (
        <button type="button" className={BUTTON} onClick={onDisconnect} data-testid="agents.item.disconnect">
          {t('agents.disconnect')}
        </button>
      )}
    </li>
  );
}

/** `null`: closed; `all`: every agent; else the agent to disconnect. */
type Target = null | 'all' | AgentEntry;

export function AgentsSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [params] = useSearchParams();
  const hint = params.get('agent');
  const agents = useStore((state) => state.agents, app.store);
  const [target, setTarget] = useState<Target>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const load = useCallback(() => void loadAgents(app.tasks), [app]);
  useEffect(load, [load]);
  const close = () => {
    setTarget(null);
    setFailure(null);
  };
  const disconnect = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await import('../../../../core/agents/revoke').then((revoke) =>
        target === 'all'
          ? revoke.revokeAllAgents(app.tasks).then(() => undefined)
          : revoke.revokeAgent(app.tasks, target!.agentId),
      );
      setTarget(null);
    } catch (error) {
      setFailure(requestErrorCode(error));
    } finally {
      setBusy(false);
    }
  };
  const entries = agents.entries ?? [];
  const main = mainOrchestrator(entries);
  const history = entries.filter((entry) => entry !== main);
  const anyLive = entries.some((entry) => LIVE_STATES.includes(entry.state));
  const name = target === null || target === 'all' ? '' : (target.label?.name ?? t('agents.unknown'));
  const held =
    target === 'all'
      ? entries.some((entry) => LIVE_STATES.includes(entry.state) && entry.heldIdentity)
      : target?.heldIdentity === true;
  return (
    <div className="flex flex-col gap-3" data-testid="agents.section">
      {(hint === 'connected' || hint === 'failed') && (
        <p role="status" className="rounded border border-gray-300 p-3" data-testid={`agents.hint.${hint}`}>
          {t(`agents.hint.${hint}`)}
        </p>
      )}
      {agents.status === 'error' && (
        <div role="alert" className="flex flex-wrap items-center gap-2" data-testid="agents.load-failed">
          <span>{t('agents.load-failed', { reason: reason(agents.code!) })}</span>
          <button type="button" className="min-h-11 underline" onClick={load} data-testid="agents.retry">
            {t('agents.retry')}
          </button>
        </div>
      )}
      {agents.entries === null && agents.status !== 'error' && (
        <p data-testid="agents.loading">{t('agents.loading')}</p>
      )}
      {agents.entries !== null && entries.length === 0 && <p data-testid="agents.empty">{t('agents.empty')}</p>}
      {entries.length > 0 && (
        <section className="flex flex-col gap-1" data-testid="agents.main">
          <h3 className="font-semibold">{t('agents.main.title')}</h3>
          <p className="text-sm text-gray-700">{t('agents.main.about')}</p>
          {main === null ? (
            <p data-testid="agents.main.none">{t('agents.main.none')}</p>
          ) : (
            <ul data-testid="agents.list">
              <Item entry={main} onDisconnect={() => setTarget(main)} />
            </ul>
          )}
        </section>
      )}
      {history.length > 0 && (
        <section className="flex flex-col gap-1" data-testid="agents.history">
          <h3 className="font-semibold">{t('agents.history.title')}</h3>
          <ul className="flex flex-col divide-y divide-gray-200">
            {history.map((entry) => (
              <Item key={entry.agentId} entry={entry} onDisconnect={() => setTarget(entry)} />
            ))}
          </ul>
        </section>
      )}
      {anyLive && (
        <button type="button" className={DANGER} onClick={() => setTarget('all')} data-testid="agents.disconnect-all">
          {t('agents.disconnect-all')}
        </button>
      )}
      <SecurityActivity />
      {target !== null && (
        <Dialog
          title={target === 'all' ? t('agents.dialog.all-title') : t('agents.dialog.title', { name })}
          testId="agents.dialog"
          busy={busy}
          onClose={close}
        >
          <p data-testid="agents.dialog.body">
            {target === 'all' ? t('agents.dialog.all-body') : t('agents.dialog.body')}
          </p>
          {held && (
            <p className="font-semibold" data-testid="agents.dialog.held">
              {t('agents.dialog.held')}
            </p>
          )}
          {busy && (
            <p role="status" data-testid="agents.dialog.cleaning">
              {t('agents.dialog.cleaning')}
            </p>
          )}
          {failure !== null && (
            <p role="alert" data-testid="agents.dialog.failed" data-code={failure}>
              {t('agents.dialog.failed', { reason: reason(failure) })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={DANGER}
              onClick={() => void disconnect()}
              disabled={busy}
              data-testid="agents.dialog.confirm"
            >
              {target === 'all' ? t('agents.disconnect-all') : t('agents.disconnect')}
            </button>
            <button type="button" className={BUTTON} onClick={close} disabled={busy} data-testid="agents.dialog.cancel">
              {t('agents.dialog.cancel')}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export const section: SettingsSection = { id: 'agents', order: 20, titleKey: 'agents.title', Component: AgentsSection };
