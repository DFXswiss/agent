// Monitoring of an organization (06-web.md section 9.1, route `/o/:org/monitoring`, right org:member; 08-monitoring.md
// section 6): the org's devices with CPU, RAM, disk, network, uptime, reachability, state, free capacity and the
// `capacity_low` badge; a drill-down to the VMs of the workgroups the user reads, then to their sessions (CPU, memory,
// task, workspace, state, tokens, cost, context share) and below a session its work VMs (KOR-29: CPU, memory, disk,
// network, runtime). Every level opens its charts. The hub decides what is visible
// (a shared platform device shows only its device totals, never another org's VMs or sessions).
//
// Live: while the screen is open the org's channel carries `metrics` frames (`AppSession.watchMonitoring`); values
// older than 60 s are greyed with their age (counted every second). The overview is read again every `POLL_MS` and
// when a live sample names an entity it does not list yet (at most every `RELOAD_MS`).
//
// Volume (ADR-086 sections 1 and 2): the tree is flattened into rows (`tree.ts`, memoized on the overview, the open
// nodes, the names and the titles) and shown through `WindowedList` (`TREE_SIZE` rows at first, `TREE_PAGE` per move,
// at most `TREE_MAX_ROWS`); rows are memoized on their content. The ages count in their own cells (one shared second
// clock); the tree itself is rebuilt only when an entity turns stale. The task titles come from one selector for the
// whole list. A hub that cuts the overview at 2 000 sessions says so with `sessions_total`/`truncated`,
// one that cuts the devices at 10 000 with `devices_truncated`.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { formatNumber } from '../../../core/i18n';
import { buildTree, type MonitoredSession, type Overview } from '../../../core/monitoring/overview';
import { selectDeviceLabels, DEVICE_PROFILE } from '../../../core/org/devices';
import { linkSessions, loadOverview, orgMonitoring, RELOAD_MS } from '../../../core/store/slices/monitoring';
import type { Selector, StoreState } from '../../../core/store/store';
import { selectTitle } from '../../../core/tasks/directory';
import { titles as pullTitles } from '../../../core/tasks/list';
import { pullOnce } from '../../../core/tasks/pull';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { TreeContext, TreeRowView, treeRowAttributes } from './rows';
import { nextStaleAt, rowKey, sameRow, treeRows } from './tree';

/** The overview is read again this often (new devices, VMs, sessions and presence). */
export const POLL_MS = 60_000;

/** Rows of the tree shown at first, added per move, rendered at most. */
export const TREE_SIZE = 100;
export const TREE_PAGE = 50;
export const TREE_MAX_ROWS = 200;

/** Task ID → verified title of the overview's sessions: one selector for the whole list, stable while unchanged. */
export function selectTitles(sessions: readonly MonitoredSession[]): Selector<ReadonlyMap<string, string>> {
  let events: StoreState['events'] | null = null;
  let last: ReadonlyMap<string, string> = new Map();
  return (state) => {
    if (state.events === events) return last;
    events = state.events;
    const next = new Map<string, string>();
    for (const item of sessions) {
      const title = selectTitle(item.wg_id, item.task_id, null)(state);
      if (title.status === 'verified') next.set(item.task_id, title.text);
    }
    if (next.size !== last.size || [...next].some(([task, text]) => last.get(task) !== text)) last = next;
    return last;
  };
}

const NO_SESSIONS: readonly MonitoredSession[] = [];

/**
 * The time the tree's staleness is computed at: moves on when the next fresh entity turns stale (a timer), not every
 * second; an entity already stale at the shown time is caught at once.
 */
function useStaleNow(overview: Overview | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (overview === null) return;
    const at = nextStaleAt([...overview.devices, ...overview.vms, ...overview.sessions, ...overview.workvms], now);
    if (at === null) return;
    const timer = setTimeout(() => setNow(Math.max(at, Date.now())), Math.max(0, at - Date.now()));
    return () => clearTimeout(timer);
  }, [overview, now]);
  return now;
}

/** Live `metrics` frames and the overview reads while the screen is open (module comment). */
function useMonitoring(org: string): void {
  const { app } = useAppEnv();
  const unknown = useStore((state) => orgMonitoring(state.monitoring, org).unknown, app.store);
  const loadedAt = useStore((state) => orgMonitoring(state.monitoring, org).loadedAt, app.store);
  useEffect(() => app.watchMonitoring(org), [app, org]);
  useEffect(() => {
    void loadOverview(app.tasks, org);
    const timer = setInterval(() => void loadOverview(app.tasks, org), POLL_MS);
    return () => clearInterval(timer);
  }, [app, org]);
  useEffect(() => {
    if (!unknown) return;
    // `unknown` is only set on a loaded overview, so `loadedAt` is known
    const wait = Math.max(0, loadedAt! + RELOAD_MS - app.tasks.now());
    const timer = setTimeout(() => void loadOverview(app.tasks, org), wait);
    return () => clearTimeout(timer);
  }, [app, org, unknown, loadedAt]);
}

export function MonitoringScreen() {
  const { org } = useParams() as { org: string };
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const { state } = useOrgView(org);
  const ready = state !== null;
  const directory = useDirectory(org, ready);
  useMonitoring(org);
  const entry = useStore((s) => orgMonitoring(s.monitoring, org), app.store);
  const links = useStore((s) => s.monitoring.links, app.store);
  const labels = useStore(selectDeviceLabels(org), app.store);
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set());
  const overview = entry.overview;
  const sessions = overview?.sessions ?? NO_SESSIONS;
  const selectSessionTitles = useMemo(() => selectTitles(sessions), [sessions]);
  const titles = useStore(selectSessionTitles, app.store);
  const now = useStaleNow(overview);
  useEffect(() => {
    if (ready) pullOnce(app.tasks, org, org, { kinds: [DEVICE_PROFILE] }).catch(() => 0);
  }, [app, org, ready]);
  useEffect(() => {
    if (overview === null) return;
    void linkSessions(
      app.tasks,
      overview.sessions.map((item) => item.session_id),
    );
    if (!ready) return;
    // the titles once per list (newest `task.meta` pages per workgroup), never a pull per row
    void pullTitles(
      app.tasks,
      overview.sessions.map((item) => ({
        org_id: org,
        wg_id: item.wg_id,
        task_id: item.task_id,
        title_event_id: null,
      })),
    );
  }, [app, org, ready, overview]);
  const flip = useCallback(
    (key: string) =>
      setOpened((previous) => {
        const next = new Set(previous);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
    [],
  );
  const actions = useMemo(() => ({ org, flip }), [org, flip]);
  const tree = useMemo(() => (overview === null ? null : buildTree(overview, links)), [overview, links]);
  const workgroups = directory.workgroups;
  const rows = useMemo(
    () =>
      tree === null ? [] : treeRows({ tree, workvms: overview!.workvms, opened, labels, workgroups, titles, now }),
    [tree, overview, opened, labels, workgroups, titles, now],
  );
  const low = overview?.devices.filter((item) => item.capacity_low).length ?? 0;
  return (
    <section className="flex flex-col gap-4 p-4" data-testid="monitoring.screen">
      <h1 className="text-xl font-semibold" data-testid="monitoring.title">
        {t('monitoring.title')}
      </h1>
      {low > 0 && (
        <p className="rounded bg-red-50 p-3 text-red-900" role="status" data-testid="monitoring.capacity-warning">
          {t('monitoring.capacity-warning', { count: low })}
        </p>
      )}
      {entry.status === 'error' && (
        <p className="text-red-800" role="alert" data-testid="monitoring.error">
          {t('monitoring.failed', { code: entry.code! })}
        </p>
      )}
      {overview?.truncated === true && (
        <p className="rounded bg-gray-100 p-3 text-gray-800" data-testid="monitoring.truncated">
          {t('monitoring.truncated', {
            shown: formatNumber(sessions.length, locale),
            total: formatNumber(overview.sessions_total ?? sessions.length, locale),
          })}
        </p>
      )}
      {overview?.devices_truncated === true && (
        <p className="rounded bg-gray-100 p-3 text-gray-800" data-testid="monitoring.devices-truncated">
          {t('monitoring.devicesTruncated', { shown: formatNumber(overview.devices.length, locale) })}
        </p>
      )}
      {tree === null ? (
        entry.status === 'loading' && (
          <p className="text-gray-600" role="status" data-testid="monitoring.loading">
            {t('monitoring.loading')}
          </p>
        )
      ) : (
        <TreeContext.Provider value={actions}>
          <WindowedList
            listId="monitoring.tree"
            items={rows}
            getKey={rowKey}
            Row={TreeRowView}
            same={sameRow}
            newest="start"
            size={TREE_SIZE}
            page={TREE_PAGE}
            maxRows={TREE_MAX_ROWS}
            testId="monitoring.devices"
            testIds={{
              older: 'monitoring.tree.more',
              newer: 'monitoring.tree.above',
              newest: 'monitoring.tree.top',
              status: 'monitoring.tree.status',
            }}
            labels={{ older: 'monitoring.tree.more', newer: 'monitoring.tree.above', newest: 'monitoring.tree.top' }}
            className="flex flex-col gap-3"
            rowAttributes={treeRowAttributes}
            rowHeight="lg"
            empty={
              <p className="text-gray-600" data-testid="monitoring.empty">
                {t('monitoring.empty')}
              </p>
            }
          />
        </TreeContext.Provider>
      )}
    </section>
  );
}
