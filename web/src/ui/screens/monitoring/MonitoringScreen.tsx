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
import { useEffect, useId, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  buildTree,
  contextShare,
  costUsd,
  freshness,
  type MonitoredDevice,
  type MonitoredSession,
  type MonitoredVm,
  type MonitoredWorkvm,
  netRates,
  type Sampled,
  type Tree,
  type VmNode,
  workvmsOf,
  type Overview,
} from '../../../core/monitoring/overview';
import { share } from '../../../core/monitoring/units';
import { DEVICE_PROFILE, selectDeviceLabels } from '../../../core/org/devices';
import { linkSessions, loadOverview, orgMonitoring, RELOAD_MS } from '../../../core/store/slices/monitoring';
import { selectTitle, TITLE_KIND } from '../../../core/tasks/directory';
import { pullOnce } from '../../../core/tasks/pull';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { deviceName } from '../devices/parts';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { EntityCharts } from './EntityCharts';
import { FreshnessNote, Metric, orNone, Toggle } from './parts';
import { useUnits } from './units';

/** The overview is read again this often (new devices, VMs, sessions and presence). */
export const POLL_MS = 60_000;

type Open = (key: string) => boolean;
type Flip = (key: string) => void;

/** CPU, memory, disk and network of a sample (the shared part of every level). */
function Resources(props: { readonly item: Sampled; readonly prefix: string; readonly diskKey?: string }) {
  const { t } = useI18n();
  const units = useUnits();
  const latest = props.item.latest;
  const rates = netRates(props.item);
  const pair = (used: number | null, limit: number | null) => {
    const part = share(used, limit);
    const text = `${orNone(used, units.bytes)} / ${orNone(limit, units.bytes)}`;
    return part === null ? text : `${text} (${units.percent(part)})`;
  };
  return (
    <>
      <Metric label={t('monitoring.cpu')} testId={`${props.prefix}.cpu`}>
        {orNone(latest?.cpu_ratio ?? null, units.cores)}
      </Metric>
      <Metric label={t('monitoring.memory')} testId={`${props.prefix}.memory`}>
        {pair(latest?.mem_used_bytes ?? null, latest?.mem_limit_bytes ?? null)}
      </Metric>
      <Metric label={t(props.diskKey ?? 'monitoring.disk')} testId={`${props.prefix}.disk`}>
        {pair(latest?.disk_used_bytes ?? null, latest?.disk_limit_bytes ?? null)}
      </Metric>
      <Metric label={t('monitoring.network')} testId={`${props.prefix}.network`}>
        {rates === null
          ? t('monitoring.network.total', {
              rx: orNone(latest?.net_rx_bytes ?? null, units.bytes),
              tx: orNone(latest?.net_tx_bytes ?? null, units.bytes),
            })
          : t('monitoring.network.rate', { rx: units.rate(rates.rx), tx: units.rate(rates.tx) })}
      </Metric>
    </>
  );
}

function Header(props: {
  readonly item: Sampled;
  readonly now: number;
  readonly prefix: string;
  readonly children: React.ReactNode;
}) {
  const fresh = freshness(props.item, props.now);
  return (
    <div className="flex flex-wrap items-center gap-2">
      {props.children}
      <FreshnessNote freshness={fresh} testId={`${props.prefix}.age`} />
    </div>
  );
}

function Charts(props: {
  readonly kind: 'device' | 'vm' | 'session' | 'workvm';
  readonly id: string;
  open: Open;
  flip: Flip;
}) {
  const { t } = useI18n();
  const panel = useId();
  const key = `charts ${props.kind} ${props.id}`;
  return (
    <>
      <Toggle
        open={props.open(key)}
        onToggle={() => props.flip(key)}
        testId={`monitoring.${props.kind}.charts-toggle`}
        controls={panel}
      >
        {t(props.open(key) ? 'monitoring.charts.hide' : 'monitoring.charts.show')}
      </Toggle>
      <div id={panel} className="basis-full">
        {props.open(key) && <EntityCharts kind={props.kind} id={props.id} />}
      </div>
    </>
  );
}

/** A work VM of a session (KOR-29): its own resources and runtime, no workgroup VM involved. */
function WorkvmItem(props: { readonly item: MonitoredWorkvm; readonly now: number; open: Open; flip: Flip }) {
  const { t } = useI18n();
  const units = useUnits();
  const { item } = props;
  const fresh = freshness(item, props.now);
  return (
    <li
      className={`flex flex-col gap-2 border-l-2 border-amber-200 py-2 pl-3 ${fresh.stale ? 'text-gray-600' : ''}`}
      data-testid="monitoring.workvm"
      data-workvm={item.workvm_id}
      data-stale={String(fresh.stale)}
    >
      <Header item={item} now={props.now} prefix="monitoring.workvm">
        <span className="font-medium" data-testid="monitoring.workvm.name">
          {t('monitoring.workvm', { id: item.workvm_id })}
        </span>
        <span
          className="rounded bg-gray-100 px-2 text-sm"
          data-testid="monitoring.workvm.state"
          data-state={item.state}
        >
          {item.state}
        </span>
      </Header>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Resources item={item} prefix="monitoring.workvm" />
        <Metric label={t('monitoring.uptime')} testId="monitoring.workvm.uptime">
          {orNone(item.latest?.uptime_seconds ?? null, units.duration)}
        </Metric>
      </dl>
      <div className="flex flex-wrap gap-2">
        <Charts kind="workvm" id={item.workvm_id} open={props.open} flip={props.flip} />
      </div>
    </li>
  );
}

function SessionItem(props: {
  readonly org: string;
  readonly item: MonitoredSession;
  readonly overview: Overview;
  readonly now: number;
  open: Open;
  flip: Flip;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const units = useUnits();
  const { item } = props;
  const title = useStore(selectTitle(item.wg_id, item.task_id, null), app.store);
  const fresh = freshness(item, props.now);
  const usage = item.usage;
  const context = usage === null ? null : contextShare(usage);
  const workvms = workvmsOf(props.overview, item.session_id);
  return (
    <li
      className={`flex flex-col gap-2 border-l-2 border-gray-200 py-2 pl-3 ${fresh.stale ? 'text-gray-600' : ''}`}
      data-testid="monitoring.session"
      data-session={item.session_id}
      data-stale={String(fresh.stale)}
    >
      <Header item={item} now={props.now} prefix="monitoring.session">
        <Link
          className="inline-flex min-h-11 items-center font-medium underline"
          to={`/o/${encodeURIComponent(props.org)}/t/${encodeURIComponent(item.task_id)}`}
          data-testid="monitoring.session.task"
        >
          {title.status === 'verified' ? title.text : t('monitoring.session.task', { id: item.task_id.slice(-6) })}
        </Link>
        <span
          className="rounded bg-gray-100 px-2 text-sm"
          data-testid="monitoring.session.state"
          data-state={item.state}
        >
          {t(`task.state.${item.state}`)}
        </span>
      </Header>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Resources item={item} prefix="monitoring.session" diskKey="monitoring.workspace" />
        <Metric label={t('monitoring.tokens')} testId="monitoring.session.tokens">
          {usage === null
            ? '–'
            : t('monitoring.tokens.value', {
                input: units.number(usage.input_tokens),
                output: units.number(usage.output_tokens),
              })}
        </Metric>
        <Metric label={t('monitoring.cost')} testId="monitoring.session.cost">
          {usage === null ? '–' : units.money(costUsd(usage))}
        </Metric>
        <Metric label={t('monitoring.context')} testId="monitoring.session.context">
          {orNone(context, units.percent)}
        </Metric>
      </dl>
      <div className="flex flex-wrap gap-2">
        <Charts kind="session" id={item.session_id} open={props.open} flip={props.flip} />
      </div>
      {workvms.length > 0 && (
        <ul className="flex flex-col gap-2" data-testid="monitoring.session.workvms">
          {workvms.map((workvm) => (
            <WorkvmItem key={workvm.workvm_id} item={workvm} now={props.now} open={props.open} flip={props.flip} />
          ))}
        </ul>
      )}
    </li>
  );
}

function VmItem(props: {
  readonly org: string;
  readonly node: VmNode;
  readonly overview: Overview;
  readonly now: number;
  open: Open;
  flip: Flip;
}) {
  const { t } = useI18n();
  const units = useUnits();
  const directory = useDirectory(props.org, false);
  const vm: MonitoredVm = props.node.vm;
  const fresh = freshness(vm, props.now);
  const sessionsId = useId();
  const key = `sessions ${vm.vm_id}`;
  const wgName = vm.wg_id === null ? null : (directory.workgroups.get(vm.wg_id) ?? t('monitoring.wg.unnamed'));
  return (
    <li
      className={`flex flex-col gap-2 border-l-2 border-blue-200 py-2 pl-3 ${fresh.stale ? 'text-gray-600' : ''}`}
      data-testid="monitoring.vm"
      data-vm={vm.vm_id}
      data-stale={String(fresh.stale)}
    >
      <Header item={vm} now={props.now} prefix="monitoring.vm">
        <span className="font-medium" data-testid="monitoring.vm.name">
          {vm.vm_id}
        </span>
        {wgName !== null && (
          <span className="text-sm text-gray-700" data-testid="monitoring.vm.wg">
            {wgName}
          </span>
        )}
        <span className="rounded bg-gray-100 px-2 text-sm" data-testid="monitoring.vm.state" data-state={vm.state}>
          {t(`monitoring.vm-state.${vm.state}`)}
        </span>
        <Reachable sample={vm} prefix="monitoring.vm" />
      </Header>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Resources item={vm} prefix="monitoring.vm" />
        <Metric label={t('monitoring.uptime')} testId="monitoring.vm.uptime">
          {orNone(vm.latest?.uptime_seconds ?? null, units.duration)}
        </Metric>
      </dl>
      <div className="flex flex-wrap gap-2">
        <Toggle
          open={props.open(key)}
          onToggle={() => props.flip(key)}
          testId="monitoring.vm.sessions-toggle"
          controls={sessionsId}
        >
          {t('monitoring.sessions', { count: props.node.sessions.length })}
        </Toggle>
        <Charts kind="vm" id={vm.vm_id} open={props.open} flip={props.flip} />
      </div>
      <ul id={sessionsId} className="flex flex-col gap-2">
        {props.open(key) &&
          (props.node.sessions.length === 0 ? (
            <li className="text-sm text-gray-600" data-testid="monitoring.vm.no-sessions">
              {t('monitoring.sessions.none')}
            </li>
          ) : (
            props.node.sessions.map((item) => (
              <SessionItem
                key={item.session_id}
                org={props.org}
                item={item}
                overview={props.overview}
                now={props.now}
                open={props.open}
                flip={props.flip}
              />
            ))
          ))}
      </ul>
    </li>
  );
}

function Reachable(props: { readonly sample: Sampled; readonly prefix: string }) {
  const { t } = useI18n();
  const reachable = props.sample.latest?.reachable ?? null;
  if (reachable === null) return null;
  return (
    <span className="text-sm" data-testid={`${props.prefix}.reachable`} data-reachable={String(reachable)}>
      {t(reachable ? 'monitoring.reachable' : 'monitoring.unreachable')}
    </span>
  );
}

function DeviceItem(props: {
  readonly org: string;
  readonly device: MonitoredDevice;
  readonly vms: readonly VmNode[];
  readonly overview: Overview;
  readonly label: string | null;
  readonly now: number;
  open: Open;
  flip: Flip;
}) {
  const { t } = useI18n();
  const units = useUnits();
  const { device } = props;
  const fresh = freshness(device, props.now);
  const vmsId = useId();
  const key = `vms ${device.device_id}`;
  const latest = device.latest;
  const capacity = device.capacity;
  return (
    <li
      className={`flex flex-col gap-2 rounded border border-gray-200 p-3 ${fresh.stale ? 'text-gray-600' : ''}`}
      data-testid="monitoring.device"
      data-device={device.device_id}
      data-stale={String(fresh.stale)}
    >
      <Header item={device} now={props.now} prefix="monitoring.device">
        <h2 className="font-medium" data-testid="monitoring.device.name">
          {deviceName(t, { id: device.device_id, label: props.label })}
        </h2>
        <span className="text-sm" data-testid="monitoring.device.online" data-online={String(device.online)}>
          {t(device.online ? 'monitoring.online' : 'monitoring.offline')}
        </span>
        <Reachable sample={device} prefix="monitoring.device" />
        {latest !== null && (
          <span
            className="rounded bg-gray-100 px-2 text-sm"
            data-testid="monitoring.device.state"
            data-state={latest.state}
          >
            {latest.state}
          </span>
        )}
        {device.capacity_low && (
          <span className="rounded bg-red-100 px-2 text-sm text-red-900" data-testid="monitoring.device.capacity-low">
            {t('monitoring.capacity-low')}
          </span>
        )}
      </Header>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Resources item={device} prefix="monitoring.device" />
        <Metric label={t('monitoring.uptime')} testId="monitoring.device.uptime">
          {orNone(latest?.uptime_seconds ?? null, units.duration)}
        </Metric>
        <Metric label={t('monitoring.capacity')} testId="monitoring.device.capacity">
          {t('monitoring.capacity.value', {
            free: latest?.free_vm_slots ?? capacity.vm_slots_free,
            total: capacity.vm_slots_total,
            memory: orNone(latest?.free_mem_bytes ?? null, units.bytes),
            pool: capacity.pool_ready,
          })}
        </Metric>
      </dl>
      <div className="flex flex-wrap gap-2">
        <Toggle
          open={props.open(key)}
          onToggle={() => props.flip(key)}
          testId="monitoring.device.vms-toggle"
          controls={vmsId}
        >
          {t('monitoring.vms', { count: props.vms.length })}
        </Toggle>
        <Charts kind="device" id={device.device_id} open={props.open} flip={props.flip} />
      </div>
      <ul id={vmsId} className="flex flex-col gap-2">
        {props.open(key) &&
          (props.vms.length === 0 ? (
            <li className="text-sm text-gray-600" data-testid="monitoring.device.no-vms">
              {t('monitoring.vms.none')}
            </li>
          ) : (
            props.vms.map((node) => (
              <VmItem
                key={node.vm.vm_id}
                org={props.org}
                node={node}
                overview={props.overview}
                now={props.now}
                open={props.open}
                flip={props.flip}
              />
            ))
          ))}
      </ul>
    </li>
  );
}

/** VMs whose device is not listed and sessions without a listed VM. */
function Others(props: {
  readonly org: string;
  readonly tree: Tree;
  readonly overview: Overview;
  readonly now: number;
  open: Open;
  flip: Flip;
}) {
  const { t } = useI18n();
  const { tree } = props;
  if (tree.vms.length + tree.sessions.length === 0) return null;
  return (
    <section className="flex flex-col gap-2" data-testid="monitoring.other">
      <h2 className="font-medium">{t('monitoring.other')}</h2>
      <ul className="flex flex-col gap-2">
        {tree.vms.map((node) => (
          <VmItem
            key={node.vm.vm_id}
            org={props.org}
            node={node}
            overview={props.overview}
            now={props.now}
            open={props.open}
            flip={props.flip}
          />
        ))}
        {tree.sessions.map((item) => (
          <SessionItem
            key={item.session_id}
            org={props.org}
            item={item}
            overview={props.overview}
            now={props.now}
            open={props.open}
            flip={props.flip}
          />
        ))}
      </ul>
    </section>
  );
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
  const { t } = useI18n();
  const { state } = useOrgView(org);
  const ready = state !== null;
  useDirectory(org, ready);
  useMonitoring(org);
  const now = useNow(true, 1000);
  const entry = useStore((s) => orgMonitoring(s.monitoring, org), app.store);
  const links = useStore((s) => s.monitoring.links, app.store);
  const labels = useStore(selectDeviceLabels(org), app.store);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const overview = entry.overview;
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
    const tasks = new Map(overview.sessions.map((item) => [item.task_id, item.wg_id]));
    for (const [task, wg] of tasks) pullOnce(app.tasks, org, wg, { kinds: [TITLE_KIND], task }).catch(() => 0);
  }, [app, org, ready, overview]);
  const open: Open = (key) => opened.has(key);
  const flip: Flip = (key) =>
    setOpened((previous) => {
      const next = new Set(previous);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  const tree = overview === null ? null : buildTree(overview, links);
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
      {tree === null ? (
        entry.status === 'loading' && (
          <p className="text-gray-600" role="status" data-testid="monitoring.loading">
            {t('monitoring.loading')}
          </p>
        )
      ) : tree.devices.length === 0 && tree.vms.length === 0 && tree.sessions.length === 0 ? (
        <p className="text-gray-600" data-testid="monitoring.empty">
          {t('monitoring.empty')}
        </p>
      ) : (
        <>
          <ul className="flex flex-col gap-3" data-testid="monitoring.devices">
            {tree.devices.map((node) => (
              <DeviceItem
                key={node.device.device_id}
                org={org}
                device={node.device}
                vms={node.vms}
                overview={overview!}
                label={labels.get(node.device.device_id) ?? null}
                now={now}
                open={open}
                flip={flip}
              />
            ))}
          </ul>
          <Others org={org} tree={tree} overview={overview!} now={now} open={open} flip={flip} />
        </>
      )}
    </section>
  );
}
