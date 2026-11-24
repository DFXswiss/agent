// The rows of the monitoring tree (06-web.md section 9.1): a device, a VM, a session, a work VM, the "none" note under an open
// node and the heading of the unlinked entries. A row renders only what its `TreeRow` carries (memoized by
// `WindowedList` with `sameRow`); the screen gives the org and the toggle through `TreeContext`, the ages count in
// their own cells (`Age`).
import { createContext, type ReactNode, useContext, useId } from 'react';
import { Link } from 'react-router-dom';
import { contextShare, costUsd, netRates, type Sampled } from '../../../core/monitoring/overview';
import { share } from '../../../core/monitoring/units';
import { useI18n } from '../../i18n';
import { NoName } from '../../names/names';
import type { RowAttributes, RowProps } from '../../lists/WindowedList';
import { deviceName } from '../devices/parts';
import { EntityCharts } from './EntityCharts';
import { Age, Metric, orNone, Toggle } from './parts';
import {
  chartsKey,
  type DeviceRow,
  type NoneRow,
  type SessionRow,
  sessionsKey,
  type TreeRow,
  type VmRow,
  vmsKey,
  type WorkvmRow,
} from './tree';
import { useUnits } from './units';

export interface TreeActions {
  readonly org: string;
  /** Opens or closes a drill-down or chart panel (`tree.ts` keys). */
  readonly flip: (key: string) => void;
}

export const TreeContext = createContext<TreeActions | null>(null);

const useTree = (): TreeActions => useContext(TreeContext)!;

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

function Header(props: { readonly item: Sampled; readonly prefix: string; readonly children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {props.children}
      <Age item={props.item} testId={`${props.prefix}.age`} />
    </div>
  );
}

function Charts(props: {
  readonly kind: 'device' | 'vm' | 'session' | 'workvm';
  readonly id: string;
  readonly open: boolean;
}) {
  const { t } = useI18n();
  const { flip } = useTree();
  const panel = useId();
  return (
    <>
      <Toggle
        open={props.open}
        onToggle={() => flip(chartsKey(props.kind, props.id))}
        testId={`monitoring.${props.kind}.charts-toggle`}
        controls={panel}
      >
        {t(props.open ? 'monitoring.charts.hide' : 'monitoring.charts.show')}
      </Toggle>
      <div id={panel} className="basis-full">
        {props.open && <EntityCharts kind={props.kind} id={props.id} />}
      </div>
    </>
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

function SessionView({ row }: { readonly row: SessionRow }) {
  const { t } = useI18n();
  const { org } = useTree();
  const units = useUnits();
  const { item } = row;
  const usage = item.usage;
  const context = usage === null ? null : contextShare(usage);
  return (
    <>
      <Header item={item} prefix="monitoring.session">
        <Link
          className="inline-flex min-h-11 items-center font-medium underline"
          to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(item.task_id)}`}
          data-testid="monitoring.session.task"
        >
          {row.title ?? <NoName t={t} text={t('names.task.unknown')} id={item.task_id} />}
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
        <Charts kind="session" id={item.session_id} open={row.charts} />
      </div>
      {row.workvms > 0 && (
        <p className="text-sm text-gray-700" data-testid="monitoring.session.workvms">
          {t('monitoring.workvms', { count: row.workvms })}
        </p>
      )}
    </>
  );
}

/** A work VM of a session (KOR-29): its own resources and runtime, no workgroup VM involved. */
function WorkvmView({ row }: { readonly row: WorkvmRow }) {
  const { t } = useI18n();
  const units = useUnits();
  const { item } = row;
  return (
    <>
      <Header item={item} prefix="monitoring.workvm">
        <span className="font-medium" data-testid="monitoring.workvm.name">
          <NoName t={t} text={t('monitoring.workvm')} id={item.workvm_id} showId />
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
        <Charts kind="workvm" id={item.workvm_id} open={row.charts} />
      </div>
    </>
  );
}

function VmView({ row }: { readonly row: VmRow }) {
  const { t } = useI18n();
  const { flip } = useTree();
  const units = useUnits();
  const vm = row.item;
  return (
    <>
      <Header item={vm} prefix="monitoring.vm">
        <span className="font-medium" data-testid="monitoring.vm.name">
          {vm.vm_id}
        </span>
        {vm.wg_id !== null && (
          <span className="text-sm text-gray-700" data-testid="monitoring.vm.wg">
            {row.wgName ?? t('monitoring.wg.unnamed')}
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
        <Toggle open={row.open} onToggle={() => flip(sessionsKey(vm.vm_id))} testId="monitoring.vm.sessions-toggle">
          {t('monitoring.sessions', { count: row.count })}
        </Toggle>
        <Charts kind="vm" id={vm.vm_id} open={row.charts} />
      </div>
    </>
  );
}

function DeviceView({ row }: { readonly row: DeviceRow }) {
  const { t } = useI18n();
  const { flip } = useTree();
  const units = useUnits();
  const device = row.item;
  const latest = device.latest;
  const capacity = device.capacity;
  return (
    <>
      <Header item={device} prefix="monitoring.device">
        <h2 className="font-medium" data-testid="monitoring.device.name">
          {deviceName(t, { id: device.device_id, label: row.label })}
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
        <Toggle open={row.open} onToggle={() => flip(vmsKey(device.device_id))} testId="monitoring.device.vms-toggle">
          {t('monitoring.vms', { count: row.count })}
        </Toggle>
        <Charts kind="device" id={device.device_id} open={row.charts} />
      </div>
    </>
  );
}

function NoneView({ row }: { readonly row: NoneRow }) {
  const { t } = useI18n();
  return <>{t(row.of === 'device' ? 'monitoring.vms.none' : 'monitoring.sessions.none')}</>;
}

function OtherView() {
  const { t } = useI18n();
  return <h2 className="pt-2 font-medium">{t('monitoring.other')}</h2>;
}

/** One row of the tree (the `Row` of `WindowedList`). */
export function TreeRowView({ item: row }: RowProps<TreeRow>) {
  switch (row.kind) {
    case 'device':
      return <DeviceView row={row} />;
    case 'vm':
      return <VmView row={row} />;
    case 'session':
      return <SessionView row={row} />;
    case 'workvm':
      return <WorkvmView row={row} />;
    case 'none':
      return <NoneView row={row} />;
    case 'other':
      return <OtherView />;
  }
}

/** Indentation per level (static classes: Tailwind compiles them from the source). */
const INDENT = ['', 'ml-4 sm:ml-8', 'ml-8 sm:ml-16', 'ml-12 sm:ml-24'] as const;

const greyed = (stale: boolean): string => (stale ? 'text-gray-600' : '');

/** The `<li>` of a row: its classes and the test hooks of the former nested lists. */
export function treeRowAttributes(row: TreeRow): RowAttributes {
  const indent = INDENT[row.level]!;
  switch (row.kind) {
    case 'device':
      return {
        className: `flex flex-col gap-2 rounded border border-gray-200 p-3 ${greyed(row.stale)}`,
        'data-testid': 'monitoring.device',
        'data-device': row.item.device_id,
        'data-stale': String(row.stale),
      };
    case 'vm':
      return {
        className: `flex flex-col gap-2 border-l-2 border-blue-200 py-2 pl-3 ${indent} ${greyed(row.stale)}`,
        'data-testid': 'monitoring.vm',
        'data-vm': row.item.vm_id,
        'data-stale': String(row.stale),
      };
    case 'session':
      return {
        className: `flex flex-col gap-2 border-l-2 border-gray-200 py-2 pl-3 ${indent} ${greyed(row.stale)}`,
        'data-testid': 'monitoring.session',
        'data-session': row.item.session_id,
        'data-stale': String(row.stale),
      };
    case 'workvm':
      return {
        className: `flex flex-col gap-2 border-l-2 border-amber-200 py-2 pl-3 ${indent} ${greyed(row.stale)}`,
        'data-testid': 'monitoring.workvm',
        'data-workvm': row.item.workvm_id,
        'data-session': row.item.session_id,
        'data-stale': String(row.stale),
      };
    case 'none':
      return row.of === 'device'
        ? {
            className: `text-sm text-gray-600 ${indent}`,
            'data-testid': 'monitoring.device.no-vms',
            'data-device': row.id,
          }
        : {
            className: `text-sm text-gray-600 ${indent}`,
            'data-testid': 'monitoring.vm.no-sessions',
            'data-vm': row.id,
          };
    case 'other':
      return { 'data-testid': 'monitoring.other' };
  }
}
