// Tab "Sessions" of a task (06-web.md section 5.3): the tree of the task's sessions – main sessions and their
// subagent children by `parent_session_id`, depth ≤ 2 – with state, "Triggered by <name>" (the person whose
// instruction created the session, ADR-083), device (its label, `ui/screens/devices/labels.ts`), model and usage
// (tokens and cost of the session, slice `usage`); each node links to the session detail `/o/:org/s/:session`
// (terminal, session chat, children).
//
// Many sessions (ADR-086 section 1): the tree is flattened depth first (the depth indents a row) into a
// `WindowedList` (`WINDOW` rows at first, `PAGE` per move, at most `MAX_ROWS`); one selector delivers the task's
// sessions and their usage (the same value while neither changed, so other tasks' sessions do not re-render the tab);
// the device names and the presence of the task's devices are read once for the list, and the rows are memoized on
// their session, usage, device name and reachability (only the runtime of a running session ticks inside its row).

import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { formatMoney, formatNumber } from '../../../core/i18n';
import { formatDuration } from '../../../core/monitoring/units';
import { runtimeSeconds } from '../../../core/sessions/runtime';
import { flatten, sessionMeta, type SessionNode, sessionTree } from '../../../core/sessions/tree';
import type { SessionEntry, TasksState } from '../../../core/store/slices/tasks';
import type { SessionUsage } from '../../../core/store/slices/usage';
import type { StoreState } from '../../../core/store/store';
import { useNow } from '../../countdown/useNow';
import { sessionVm } from '../../../core/tasks/placement';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { type RowAttributes, WindowedList } from '../../lists/WindowedList';
import { useDeviceNames } from '../../screens/devices/labels';
import { useDevicePresence } from '../../screens/devices/presence';
import { useAppEnv } from '../../shell/context';
import type { TaskTabProps } from '../../registries/task-tabs';
import '../../../core/i18n/lazy/model';
import { TriggeredBy } from '../people';

/** Rows rendered at first, rows added by a move, the cap. */
export const WINDOW = 100;
export const PAGE = 50;
export const MAX_ROWS = 200;

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

/** The runtime of a session; a running one is redrawn every `RUNTIME_TICK_MS` (only this element). */
function Runtime({ row }: { readonly row: SessionEntry['row'] }) {
  const { t } = useI18n();
  const running = !ENDED_STATES.has(row.state);
  const now = useNow(running, RUNTIME_TICK_MS);
  const seconds = runtimeSeconds(row.first_working_at ?? row.created_at, row.ended_at, running, now);
  return seconds === null ? null : (
    <span data-testid="session.runtime">{t('task.sessions.runtime', { duration: formatDuration(seconds) })}</span>
  );
}

/** What a row shows besides the session itself: its device's name and reachability and its usage. */
interface Shown {
  /** The device's name with the session's VM, null without a device. */
  readonly device: string | null;
  /** Whether the hub reaches the device; undefined while unknown. */
  readonly reachable: boolean | undefined;
  readonly usage: SessionUsage | null;
}

/** The device (by name, and whether the hub reaches it), the runtime, the model and the usage counter of a session. */
function Facts({ entry, device, reachable, usage }: Shown & { readonly entry: SessionEntry }) {
  const { t, locale } = useI18n();
  const model = sessionMeta(entry).modelId;
  return (
    <span className="flex flex-wrap gap-x-3 text-xs text-gray-600">
      <span data-testid="session.device">
        {device === null ? t('task.sessions.device.none') : t('task.sessions.device', { device })}
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
      <Runtime row={entry.row} />
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

/** The facts of one session read from the store and the hub (the session screens; the tab reads them for all rows). */
export function SessionFacts({ node }: { readonly node: Pick<SessionNode, 'entry'> }) {
  const { app } = useAppEnv();
  const row = node.entry.row;
  const name = useDeviceNames([row.org_id]);
  const presence = useDevicePresence(row.org_id, row.device_id === null ? [] : [row.device_id]);
  const usage = useStore((state) => state.usage.bySession[row.session_id]?.usage ?? null, app.store);
  return (
    <Facts
      entry={node.entry}
      device={row.device_id === null ? null : placementText(name(row.device_id), sessionVm(row))}
      reachable={row.device_id === null ? undefined : presence?.get(row.device_id)}
      usage={usage}
    />
  );
}

type Sessions = TasksState['sessions'];
type Usages = Readonly<Record<string, SessionUsage | null>>;

/** What the tab shows of its task: the task's sessions and the usage of each. */
export interface TaskSessions {
  readonly sessions: Sessions;
  readonly usage: Usages;
}

function sameRecord<T>(a: Readonly<Record<string, T>>, b: Readonly<Record<string, T>>): boolean {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((id) => id in b && a[id] === b[id]);
}

/** The sessions of `task` and their usage; the same value while neither changed (module comment). */
export function selectTaskSessions(task: string): (state: StoreState) => TaskSessions {
  let last: TaskSessions = { sessions: {}, usage: {} };
  let input: readonly unknown[] = [];
  return (state) => {
    // other changes of the store pass without a look at the sessions
    if (input[0] === state.tasks.sessions && input[1] === state.usage.bySession) return last;
    input = [state.tasks.sessions, state.usage.bySession];
    const sessions: Record<string, SessionEntry> = {};
    const usage: Record<string, SessionUsage | null> = {};
    for (const [id, entry] of Object.entries(state.tasks.sessions)) {
      if (entry.row.task_id !== task) continue;
      sessions[id] = entry;
      usage[id] = state.usage.bySession[id]?.usage ?? null;
    }
    const keep = sameRecord(sessions, last.sessions);
    const keepUsage = sameRecord(usage, last.usage);
    if (keep && keepUsage) return last;
    last = { sessions: keep ? last.sessions : sessions, usage: keepUsage ? last.usage : usage };
    return last;
  };
}

interface Item extends Shown {
  readonly node: SessionNode;
  readonly org: string;
}

function Node({ item }: { readonly item: Item }) {
  const { t } = useI18n();
  const { node, org } = item;
  const id = node.entry.row.session_id;
  return (
    <Link
      className="flex min-h-11 flex-wrap items-center gap-2 rounded px-2 py-1 hover:bg-gray-50"
      to={`/o/${encodeURIComponent(org)}/s/${encodeURIComponent(id)}`}
      data-testid="sessions.link"
    >
      <span className="font-medium">
        {node.depth === 0 ? t('task.sessions.main') : t('task.chat.subagent', { path: node.path.join('.') })}
      </span>
      <SessionState state={node.entry.row.state} />
      <Facts entry={node.entry} device={item.device} reachable={item.reachable} usage={item.usage} />
      <TriggeredBy org={org} user={node.entry.row.triggered_by} testId="session.triggered-by" />
    </Link>
  );
}

export const sameItem = (a: Item, b: Item): boolean =>
  a.node.entry === b.node.entry &&
  a.node.depth === b.node.depth &&
  a.node.path.join('.') === b.node.path.join('.') &&
  a.org === b.org &&
  a.device === b.device &&
  a.reachable === b.reachable &&
  a.usage === b.usage;

const key = (item: Item): string => item.node.entry.row.session_id;

/** Indentation of a subagent row by its depth (static classes; the depth is at most `MAX_DEPTH`). */
const INDENT: readonly string[] = ['', 'ml-4 border-l-2 border-gray-200 pl-3', 'ml-8 border-l-2 border-gray-200 pl-3'];

const attributes = (item: Item): RowAttributes => ({
  className: INDENT[item.node.depth]!,
  'data-testid': 'sessions.node',
  'data-session': key(item),
  'data-depth': item.node.depth,
});

export function SessionsTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const own = useStore(
    useMemo(() => selectTaskSessions(taskId), [taskId]),
    app.store,
  );
  const name = useDeviceNames([orgId]);
  const nodes = useMemo(() => flatten(sessionTree(own.sessions, taskId)), [own.sessions, taskId]);
  // the devices of the task's sessions (a handful): their presence is read once for the list
  const devices = useMemo(
    () => nodes.flatMap((node) => (node.entry.row.device_id === null ? [] : [node.entry.row.device_id])),
    [nodes],
  );
  const presence = useDevicePresence(orgId, devices);
  const items = useMemo(
    () =>
      nodes.map((node): Item => {
        const row = node.entry.row;
        return {
          node,
          org: orgId,
          device: row.device_id === null ? null : placementText(name(row.device_id), sessionVm(row)),
          reachable: row.device_id === null ? undefined : presence?.get(row.device_id),
          usage: own.usage[row.session_id]!,
        };
      }),
    [nodes, orgId, name, presence, own.usage],
  );
  return (
    <section aria-label={t('task.sessions.label')} data-testid="sessions">
      <WindowedList
        listId="task.sessions"
        items={items}
        getKey={key}
        Row={Node}
        same={sameItem}
        newest="start"
        size={WINDOW}
        page={PAGE}
        maxRows={MAX_ROWS}
        as="ul"
        className="flex flex-col gap-2"
        rowHeight="sm"
        rowAttributes={attributes}
        testId="sessions.tree"
        empty={
          <p className="text-sm text-gray-600" data-testid="sessions.empty">
            {t('task.sessions.empty')}
          </p>
        }
      />
    </section>
  );
}
