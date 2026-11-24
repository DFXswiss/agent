// The monitoring tree as flat rows (06-web.md section 9.1, ADR-086 section 1): device → VM → session → work VM
// (KOR-29) in today's order, the children of a node only while it is open (the work VMs of a session whenever the
// session is shown), then the "not linked" group. Each row carries everything it
// renders (names, title, open flags, staleness), so a memoized row re-renders only when its own content changed; the
// age of a value is the only thing that changes every second and is counted by the row's time cell itself.
import {
  freshness,
  type MonitoredDevice,
  type MonitoredSession,
  type MonitoredVm,
  type MonitoredWorkvm,
  type Sampled,
  STALE_MS,
  type Tree,
  type VmNode,
} from '../../../core/monitoring/overview';

interface Base {
  readonly key: string;
  /** Depth in the tree (0 device or unlinked entry, 1 VM of a device, 2 session of such a VM, 3 its work VM). */
  readonly level: number;
}

export interface DeviceRow extends Base {
  readonly kind: 'device';
  readonly item: MonitoredDevice;
  readonly label: string | null;
  readonly count: number;
  readonly open: boolean;
  readonly charts: boolean;
  readonly stale: boolean;
}

export interface VmRow extends Base {
  readonly kind: 'vm';
  readonly item: MonitoredVm;
  /** Name of the VM's workgroup; null when it has none or the name is not known. */
  readonly wgName: string | null;
  readonly count: number;
  readonly open: boolean;
  readonly charts: boolean;
  readonly stale: boolean;
}

export interface SessionRow extends Base {
  readonly kind: 'session';
  readonly item: MonitoredSession;
  /** The verified task title, null while none is known. */
  readonly title: string | null;
  /** Number of the session's work VMs (their rows follow the session's row). */
  readonly workvms: number;
  readonly charts: boolean;
  readonly stale: boolean;
}

/** A work VM of a session (KOR-29): its own resources and runtime. */
export interface WorkvmRow extends Base {
  readonly kind: 'workvm';
  readonly item: MonitoredWorkvm;
  readonly charts: boolean;
  readonly stale: boolean;
}

/** "No VMs" under an open device, "No sessions" under an open VM. */
export interface NoneRow extends Base {
  readonly kind: 'none';
  readonly of: 'device' | 'vm';
  readonly id: string;
}

/** The heading of the entries not linked to a listed device or VM. */
export interface OtherRow extends Base {
  readonly kind: 'other';
}

export type TreeRow = DeviceRow | VmRow | SessionRow | WorkvmRow | NoneRow | OtherRow;

export interface TreeInput {
  readonly tree: Tree;
  /** The overview's work VMs (shown below their sessions). */
  readonly workvms: readonly MonitoredWorkvm[];
  /** Keys of the open drill-downs and chart panels (`vmsKey`, `sessionsKey`, `chartsKey`). */
  readonly opened: ReadonlySet<string>;
  /** Device ID → label. */
  readonly labels: ReadonlyMap<string, string>;
  /** Workgroup ID → name. */
  readonly workgroups: ReadonlyMap<string, string>;
  /** Task ID → verified title. */
  readonly titles: ReadonlyMap<string, string>;
  readonly now: number;
}

export const vmsKey = (device: string): string => `vms ${device}`;
export const sessionsKey = (vm: string): string => `sessions ${vm}`;
export const chartsKey = (kind: 'device' | 'vm' | 'session' | 'workvm', id: string): string => `charts ${kind} ${id}`;

export function treeRows(input: TreeInput): TreeRow[] {
  const { tree, opened, now } = input;
  const rows: TreeRow[] = [];
  const stale = (item: Sampled) => freshness(item, now).stale;
  // the work VMs grouped once per build, in the overview's order (never a scan per session)
  const workvms = new Map<string, MonitoredWorkvm[]>();
  for (const item of input.workvms) {
    const list = workvms.get(item.session_id);
    if (list === undefined) workvms.set(item.session_id, [item]);
    else list.push(item);
  }
  const session = (item: MonitoredSession, level: number) => {
    const children = workvms.get(item.session_id) ?? [];
    rows.push({
      kind: 'session',
      key: `s ${item.session_id}`,
      level,
      item,
      title: input.titles.get(item.task_id) ?? null,
      workvms: children.length,
      charts: opened.has(chartsKey('session', item.session_id)),
      stale: stale(item),
    });
    for (const child of children)
      rows.push({
        kind: 'workvm',
        key: `w ${child.workvm_id}`,
        level: level + 1,
        item: child,
        charts: opened.has(chartsKey('workvm', child.workvm_id)),
        stale: stale(child),
      });
  };
  const vm = (node: VmNode, level: number) => {
    const item = node.vm;
    const open = opened.has(sessionsKey(item.vm_id));
    rows.push({
      kind: 'vm',
      key: `v ${item.vm_id}`,
      level,
      item,
      wgName: item.wg_id === null ? null : (input.workgroups.get(item.wg_id) ?? null),
      count: node.sessions.length,
      open,
      charts: opened.has(chartsKey('vm', item.vm_id)),
      stale: stale(item),
    });
    if (!open) return;
    if (node.sessions.length === 0)
      rows.push({ kind: 'none', key: `ns ${item.vm_id}`, level: level + 1, of: 'vm', id: item.vm_id });
    for (const child of node.sessions) session(child, level + 1);
  };
  for (const node of tree.devices) {
    const item = node.device;
    const open = opened.has(vmsKey(item.device_id));
    rows.push({
      kind: 'device',
      key: `d ${item.device_id}`,
      level: 0,
      item,
      label: input.labels.get(item.device_id) ?? null,
      count: node.vms.length,
      open,
      charts: opened.has(chartsKey('device', item.device_id)),
      stale: stale(item),
    });
    if (!open) continue;
    if (node.vms.length === 0)
      rows.push({ kind: 'none', key: `nv ${item.device_id}`, level: 1, of: 'device', id: item.device_id });
    for (const child of node.vms) vm(child, 1);
  }
  if (tree.vms.length + tree.sessions.length > 0) rows.push({ kind: 'other', key: 'other', level: 0 });
  for (const node of tree.vms) vm(node, 0);
  for (const item of tree.sessions) session(item, 0);
  return rows;
}

export const rowKey = (row: TreeRow): string => row.key;

/** Whether two builds of a row render the same: every field the same value (entities by identity). */
export function sameRow(a: TreeRow, b: TreeRow): boolean {
  const left = a as unknown as Record<string, unknown>;
  const right = b as unknown as Record<string, unknown>;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every((key) => left[key] === right[key]);
}

/** When the next fresh entity turns stale (unix ms), null if none will: the tree is rebuilt then, not every second. */
export function nextStaleAt(items: Iterable<Sampled>, now: number): number | null {
  let next: number | null = null;
  for (const item of items) {
    if (item.latest === null || item.stale) continue;
    const at = Date.parse(item.latest.ts) + STALE_MS + 1;
    if (at > now && (next === null || at < next)) next = at;
  }
  return next;
}
