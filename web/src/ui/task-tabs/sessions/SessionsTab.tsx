// Tab "Sessions" of a task (06-web.md section 5.3): the tree of the task's sessions – main sessions and their
// subagent children by `parent_session_id`, depth ≤ 2 – with state, device (its label,
// `ui/screens/devices/labels.ts`), model and usage (tokens and cost of the session, slice `usage`); each node links to
// the session detail `/o/:org/s/:session` (terminal, session chat, children).
import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { formatMoney, formatNumber } from '../../../core/i18n';
import { formatDuration } from '../../../core/monitoring/units';
import { runtimeSeconds } from '../../../core/sessions/runtime';
import { sessionMeta, type SessionNode, sessionTree } from '../../../core/sessions/tree';
import { useNow } from '../../countdown/useNow';
import { sessionVm } from '../../../core/tasks/placement';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useDeviceNames } from '../../screens/devices/labels';
import { useDevicePresence } from '../../screens/devices/presence';
import { useAppEnv } from '../../shell/context';
import type { TaskTabProps } from '../../registries/task-tabs';

/** The runtime of a running session is redrawn once a second. */
export const RUNTIME_TICK_MS = 1000;

export const STATE_BADGE: Readonly<Record<string, string>> = {
  working: 'bg-green-100 text-green-900',
  idle: 'bg-blue-100 text-blue-900',
  failed: 'bg-red-100 text-red-900',
  stopped: 'bg-gray-200 text-gray-800',
};

/** The state badge of a session. */
export function SessionState({ state }: { readonly state: string }) {
  const { t } = useI18n();
  return (
    <span
      className={`rounded px-2 text-xs ${STATE_BADGE[state] ?? 'bg-amber-100 text-amber-900'}`}
      data-testid="session.state"
      data-state={state}
    >
      {t(`task.state.${state}`)}
    </span>
  );
}

/** The states after which a session runs no more (its runtime stops at `ended_at`). */
export const ENDED_STATES: ReadonlySet<string> = new Set(['stopped', 'failed', 'suspended']);

/** "<device> · vm-…" (the VM once the session has one, ADR-075 section 6). */
export const placementText = (device: string, vm: string | null): string =>
  vm === null ? device : `${device} · ${vm}`;

/** The device (by name, and whether the hub reaches it) and VM, the runtime, the model and the usage counter of a
 * session. */
export function SessionFacts({ node }: { readonly node: Pick<SessionNode, 'entry'> }) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const row = node.entry.row;
  const name = useDeviceNames([row.org_id]);
  const presence = useDevicePresence(row.org_id);
  const model = sessionMeta(node.entry).modelId;
  const usage = useStore((state) => state.usage.bySession[row.session_id]?.usage ?? null, app.store);
  const running = !ENDED_STATES.has(row.state);
  const now = useNow(running, RUNTIME_TICK_MS);
  const seconds = runtimeSeconds(row.first_working_at ?? row.created_at, row.ended_at, running, now);
  const reachable = row.device_id === null ? undefined : presence?.get(row.device_id);
  return (
    <span className="flex flex-wrap gap-x-3 text-xs text-gray-600">
      <span data-testid="session.device">
        {row.device_id === null
          ? t('task.sessions.device.none')
          : t('task.sessions.device', { device: placementText(name(row.device_id), sessionVm(row)) })}
        {reachable !== undefined && (
          <span
            className={reachable ? 'text-green-800' : 'text-red-800'}
            data-testid="session.device.reachable"
            data-reachable={reachable}
          >
            {' '}
            {t(reachable ? 'task.sessions.device.reachable' : 'task.sessions.device.unreachable')}
          </span>
        )}
      </span>
      {seconds !== null && (
        <span data-testid="session.runtime">{t('task.sessions.runtime', { duration: formatDuration(seconds) })}</span>
      )}
      <span data-testid="session.model">
        {model === null ? t('task.sessions.model.none') : t('task.sessions.model', { model })}
      </span>
      <span data-testid="session.usage">
        {usage === null
          ? t('task.sessions.usage.none')
          : t('task.sessions.usage', {
              input: formatNumber(usage.input_tokens, locale),
              output: formatNumber(usage.output_tokens, locale),
              cost: formatMoney(usage.cost_micro_usd / 1_000_000, locale),
              basis: t(`model.usage.basis.${usage.cost_basis}`),
            })}
      </span>
    </span>
  );
}

function Node({ org, node }: { readonly org: string; readonly node: SessionNode }) {
  const { t } = useI18n();
  const id = node.entry.row.session_id;
  return (
    <li className="flex flex-col gap-1" data-testid="sessions.node" data-session={id} data-depth={node.depth}>
      <Link
        className="flex min-h-11 flex-wrap items-center gap-2 rounded px-2 py-1 hover:bg-gray-50"
        to={`/o/${encodeURIComponent(org)}/s/${encodeURIComponent(id)}`}
        data-testid="sessions.link"
      >
        <span className="font-medium">
          {node.depth === 0 ? t('task.sessions.main') : t('task.chat.subagent', { path: node.path.join('.') })}
        </span>
        <SessionState state={node.entry.row.state} />
        <SessionFacts node={node} />
      </Link>
      {node.children.length > 0 && (
        <ul className="ml-4 border-l-2 border-gray-200 pl-3">
          {node.children.map((child) => (
            <Node key={child.entry.row.session_id} org={org} node={child} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function SessionsTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const sessions = useStore((state) => state.tasks.sessions, app.store);
  const tree = useMemo(() => sessionTree(sessions, taskId), [sessions, taskId]);
  return (
    <section aria-label={t('task.sessions.label')} data-testid="sessions">
      {tree.length === 0 ? (
        <p className="text-sm text-gray-600" data-testid="sessions.empty">
          {t('task.sessions.empty')}
        </p>
      ) : (
        <ul className="flex flex-col gap-2" data-testid="sessions.tree">
          {tree.map((node) => (
            <Node key={node.entry.row.session_id} org={orgId} node={node} />
          ))}
        </ul>
      )}
    </section>
  );
}
