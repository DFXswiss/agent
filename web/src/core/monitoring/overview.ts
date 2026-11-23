// The monitoring overview (03-api.md section 4.11, 08-monitoring.md section 6): devices of the org with presence,
// capacity and `capacity_low`, VMs of readable workgroups and their sessions, each with the newest sample. The hub
// already filters by rights (a shared platform device shows only its device sample); the client only checks shapes,
// keeps the newest sample per entity from the live `metrics` frames (at-least-once, possibly repeated) and derives what
// the screen shows:
//
// - **freshness**: a value older than `STALE_MS` (60 s) is stale and shown greyed with its age; the hub's `stale` flag
//   of the load holds until a newer sample arrives, the age is counted on the client clock while the screen is open;
// - **network rates** from two consecutive samples of the counters (a counter that went back is a restart: no rate);
// - **the tree** device → VM → session: a session belongs to the VM its session row names (`vm_id`, read once per
//   session), or – while that is not known – to the only VM of its workgroup; anything else is listed separately.
import { HubError } from '../net/signedFetch';
import { isCapacity, type Capacity } from '../org/devices';
import { isRecord } from '../registry';

export const STALE_MS = 60_000;

export interface MetricSample {
  readonly ts: string;
  readonly cpu_ratio: number | null;
  readonly mem_used_bytes: number | null;
  readonly mem_limit_bytes: number | null;
  readonly disk_used_bytes: number | null;
  readonly disk_limit_bytes: number | null;
  readonly net_rx_bytes: number | null;
  readonly net_tx_bytes: number | null;
  readonly uptime_seconds: number | null;
  readonly reachable: boolean;
  readonly state: string;
  readonly free_vm_slots: number | null;
  readonly free_mem_bytes: number | null;
}

export const NUMERIC_FIELDS = [
  'cpu_ratio',
  'mem_used_bytes',
  'mem_limit_bytes',
  'disk_used_bytes',
  'disk_limit_bytes',
  'net_rx_bytes',
  'net_tx_bytes',
  'uptime_seconds',
  'free_vm_slots',
  'free_mem_bytes',
] as const;

export interface Usage {
  readonly requests: number;
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly cache_read_tokens: number;
  readonly cache_write_tokens: number;
  readonly thinking_tokens: number;
  readonly cost_micro_usd: number;
  readonly cost_basis: string;
  readonly context_used_tokens: number;
  readonly context_window_tokens: number;
  readonly last_provider: string | null;
  readonly updated_at: string;
}

const USAGE_COUNTS = [
  'requests',
  'input_tokens',
  'output_tokens',
  'cache_read_tokens',
  'cache_write_tokens',
  'thinking_tokens',
  'cost_micro_usd',
  'context_used_tokens',
  'context_window_tokens',
] as const;

/** What every entity of the overview carries: the newest sample, the one before it (for rates) and the hub's flag. */
export interface Sampled {
  readonly latest: MetricSample | null;
  readonly previous: MetricSample | null;
  readonly stale: boolean;
}

export interface MonitoredDevice extends Sampled {
  readonly device_id: string;
  readonly online: boolean;
  readonly capacity: Capacity;
  readonly capacity_low: boolean;
}

export interface MonitoredVm extends Sampled {
  readonly vm_id: string;
  readonly device_id: string;
  readonly wg_id: string | null;
  readonly state: string;
}

export interface MonitoredSession extends Sampled {
  readonly session_id: string;
  readonly task_id: string;
  readonly wg_id: string;
  readonly state: string;
  readonly usage: Usage | null;
}

export interface Overview {
  readonly devices: readonly MonitoredDevice[];
  readonly vms: readonly MonitoredVm[];
  readonly sessions: readonly MonitoredSession[];
}

export type EntityKind = 'device' | 'vm' | 'session';

const isText = (value: unknown): value is string => typeof value === 'string';
const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const isNumberOrNull = (value: unknown): boolean =>
  value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0);

export function isSample(value: unknown): value is MetricSample {
  return (
    isRecord(value) &&
    isText(value.ts) &&
    !Number.isNaN(Date.parse(value.ts)) &&
    typeof value.reachable === 'boolean' &&
    isText(value.state) &&
    NUMERIC_FIELDS.every((field) => isNumberOrNull(value[field]))
  );
}

export function isUsage(value: unknown): value is Usage {
  return (
    isRecord(value) &&
    USAGE_COUNTS.every((field) => isCount(value[field])) &&
    isText(value.cost_basis) &&
    (value.last_provider === null || isText(value.last_provider)) &&
    isText(value.updated_at)
  );
}

const latestOf = (value: unknown): MetricSample | null | undefined =>
  value === null ? null : isSample(value) ? value : undefined;

/** The sampled part of an item, or null when `latest` is malformed. */
function sampled(item: Record<string, unknown>): Sampled | null {
  const latest = latestOf(item.latest);
  if (latest === undefined) return null;
  return { latest, previous: null, stale: item.stale === true || latest === null };
}

function device(item: unknown): MonitoredDevice | null {
  if (!isRecord(item) || !isText(item.device_id) || typeof item.online !== 'boolean') return null;
  if (!isCapacity(item.capacity) || typeof item.capacity_low !== 'boolean') return null;
  const part = sampled(item);
  if (part === null) return null;
  return {
    device_id: item.device_id,
    online: item.online,
    capacity: item.capacity,
    capacity_low: item.capacity_low,
    ...part,
  };
}

function vm(item: unknown): MonitoredVm | null {
  if (!isRecord(item) || !isText(item.vm_id) || !isText(item.device_id) || !isText(item.state)) return null;
  if (item.wg_id !== null && !isText(item.wg_id)) return null;
  const part = sampled(item);
  if (part === null) return null;
  return { vm_id: item.vm_id, device_id: item.device_id, wg_id: item.wg_id, state: item.state, ...part };
}

function session(item: unknown): MonitoredSession | null {
  if (!isRecord(item) || !isText(item.session_id) || !isText(item.task_id) || !isText(item.wg_id)) return null;
  if (!isText(item.state) || (item.usage !== null && !isUsage(item.usage))) return null;
  const part = sampled(item);
  if (part === null) return null;
  const { session_id, task_id, wg_id, state, usage } = item;
  return { session_id, task_id, wg_id, state, usage, ...part };
}

function items<T>(value: unknown, parse: (item: unknown) => T | null): T[] {
  if (!Array.isArray(value)) throw new HubError('unavailable', 200);
  return value.map(parse).filter((item): item is T => item !== null);
}

/** The body of `GET /v1/monitoring/overview`; malformed items are left out, a malformed body is `unavailable`. */
export function parseOverview(body: unknown): Overview {
  if (!isRecord(body)) throw new HubError('unavailable', 200);
  return { devices: items(body.devices, device), vms: items(body.vms, vm), sessions: items(body.sessions, session) };
}

// --- live samples ---

export interface LiveSample {
  readonly entity_kind: EntityKind;
  readonly entity_id: string;
  readonly sample: MetricSample;
}

const KINDS: readonly string[] = ['device', 'vm', 'session'];

/** The samples of a `metrics` frame (`{samples:[{entity_kind, entity_id, sample}]}`); malformed ones are left out. */
export function frameSamples(data: unknown): LiveSample[] {
  if (!isRecord(data) || !Array.isArray(data.samples)) return [];
  return data.samples.filter(
    (item): item is LiveSample =>
      isRecord(item) && KINDS.includes(item.entity_kind as string) && isText(item.entity_id) && isSample(item.sample),
  );
}

const idOf = (kind: EntityKind, item: MonitoredDevice | MonitoredVm | MonitoredSession): string =>
  kind === 'device'
    ? (item as MonitoredDevice).device_id
    : kind === 'vm'
      ? (item as MonitoredVm).vm_id
      : (item as MonitoredSession).session_id;

/** `item` with `sample` as its newest sample, if it is newer than the current one (else `item` itself). */
function withSample<T extends Sampled>(item: T, sample: MetricSample): T {
  if (item.latest !== null && Date.parse(sample.ts) <= Date.parse(item.latest.ts)) return item;
  return { ...item, previous: item.latest, latest: sample, stale: false };
}

/** The key of an entity in `unknown` lists: `<kind> <id>`. */
export const entityKey = (kind: EntityKind, id: string): string => `${kind} ${id}`;

/** Whether the overview lists the entity of `key`. */
export function lists(overview: Overview, key: string): boolean {
  const [kind, id] = key.split(' ') as [EntityKind, string];
  const items: readonly (MonitoredDevice | MonitoredVm | MonitoredSession)[] =
    kind === 'device' ? overview.devices : kind === 'vm' ? overview.vms : overview.sessions;
  return items.some((item) => idOf(kind, item) === id);
}

/**
 * The overview with the live samples applied (only newer ones), and the keys of the entities a sample named that the
 * overview does not list (a new VM or session: the overview is read again).
 */
export function applySamples(
  overview: Overview,
  samples: readonly LiveSample[],
): { overview: Overview; unknown: string[] } {
  const all = { device: [...overview.devices], vm: [...overview.vms], session: [...overview.sessions] };
  const unknown: string[] = [];
  let changed = false;
  for (const { entity_kind: kind, entity_id: id, sample } of samples) {
    const list = all[kind] as (MonitoredDevice | MonitoredVm | MonitoredSession)[];
    const index = list.findIndex((item) => idOf(kind, item) === id);
    if (index < 0) {
      unknown.push(entityKey(kind, id));
      continue;
    }
    const next = withSample(list[index]!, sample);
    if (next !== list[index]) {
      list[index] = next;
      changed = true;
    }
  }
  if (!changed) return { overview, unknown };
  return { overview: { devices: all.device, vms: all.vm, sessions: all.session }, unknown };
}

const tsOf = (sample: MetricSample): number => Date.parse(sample.ts);

/** `item` of a new read with what the previous overview knew of it: a newer live sample and the one before. */
function carry<T extends Sampled>(old: T | undefined, item: T): T {
  if (old === undefined || old.latest === null) return item;
  if (item.latest === null || tsOf(item.latest) < tsOf(old.latest)) {
    return { ...item, latest: old.latest, previous: old.previous, stale: old.stale };
  }
  return { ...item, previous: tsOf(item.latest) === tsOf(old.latest) ? old.previous : old.latest };
}

/**
 * A newly read overview with the samples of the previous one carried over: a live sample newer than the read one is
 * kept, and the sample before the newest stays known (so the network rate does not fall back to totals after a read).
 */
export function carryOver(previous: Overview | null, next: Overview): Overview {
  if (previous === null) return next;
  const devices = new Map(previous.devices.map((item) => [item.device_id, item]));
  const vms = new Map(previous.vms.map((item) => [item.vm_id, item]));
  const sessions = new Map(previous.sessions.map((item) => [item.session_id, item]));
  return {
    devices: next.devices.map((item) => carry(devices.get(item.device_id), item)),
    vms: next.vms.map((item) => carry(vms.get(item.vm_id), item)),
    sessions: next.sessions.map((item) => carry(sessions.get(item.session_id), item)),
  };
}

// --- derived values ---

export interface Freshness {
  readonly stale: boolean;
  /** Age of the newest sample in ms (never negative), null without one. */
  readonly ageMs: number | null;
}

/** Stale: no sample, the hub's flag of the load, or older than `STALE_MS` at `now`. */
export function freshness(item: Sampled, now: number): Freshness {
  if (item.latest === null) return { stale: true, ageMs: null };
  const ageMs = Math.max(0, now - Date.parse(item.latest.ts));
  return { stale: item.stale || ageMs > STALE_MS, ageMs };
}

export interface Rates {
  readonly rx: number;
  readonly tx: number;
}

/** Bytes per second between the two newest samples; null without both counters or after a counter reset. */
export function netRates(item: Sampled): Rates | null {
  const { previous, latest } = item;
  if (previous === null || latest === null) return null;
  const seconds = (Date.parse(latest.ts) - Date.parse(previous.ts)) / 1000;
  const rate = (now: number | null, before: number | null): number | null =>
    now === null || before === null || now < before ? null : (now - before) / seconds;
  const rx = rate(latest.net_rx_bytes, previous.net_rx_bytes);
  const tx = rate(latest.net_tx_bytes, previous.net_tx_bytes);
  return seconds <= 0 || rx === null || tx === null ? null : { rx, tx };
}

/** The share of the context window in use, null without a window. */
export function contextShare(usage: Usage): number | null {
  return usage.context_window_tokens > 0 ? usage.context_used_tokens / usage.context_window_tokens : null;
}

/** The cost in USD. */
export const costUsd = (usage: Usage): number => usage.cost_micro_usd / 1_000_000;

// --- tree ---

export interface VmNode {
  readonly vm: MonitoredVm;
  readonly sessions: readonly MonitoredSession[];
}

export interface DeviceNode {
  readonly device: MonitoredDevice;
  readonly vms: readonly VmNode[];
}

export interface Tree {
  readonly devices: readonly DeviceNode[];
  /** VMs whose device the overview does not list. */
  readonly vms: readonly VmNode[];
  /** Sessions without a known VM of the overview. */
  readonly sessions: readonly MonitoredSession[];
}

/** Device → VM → session (module comment); `links` maps a session to the `vm_id` of its row (null: none). */
export function buildTree(overview: Overview, links: Readonly<Record<string, string | null>>): Tree {
  const vmIds = new Set(overview.vms.map((item) => item.vm_id));
  const byWg = new Map<string, MonitoredVm[]>();
  for (const item of overview.vms) {
    if (item.wg_id !== null) byWg.set(item.wg_id, [...(byWg.get(item.wg_id) ?? []), item]);
  }
  const placed = new Map<string, MonitoredSession[]>();
  const loose: MonitoredSession[] = [];
  for (const item of overview.sessions) {
    const linked = links[item.session_id];
    const only = byWg.get(item.wg_id);
    const target =
      linked !== undefined && linked !== null && vmIds.has(linked)
        ? linked
        : linked === undefined && only?.length === 1
          ? only[0]!.vm_id
          : null;
    if (target === null) loose.push(item);
    else placed.set(target, [...(placed.get(target) ?? []), item]);
  }
  const node = (item: MonitoredVm): VmNode => ({ vm: item, sessions: placed.get(item.vm_id) ?? [] });
  const deviceIds = new Set(overview.devices.map((item) => item.device_id));
  return {
    devices: overview.devices.map((item) => ({
      device: item,
      vms: overview.vms.filter((vmItem) => vmItem.device_id === item.device_id).map(node),
    })),
    vms: overview.vms.filter((item) => !deviceIds.has(item.device_id)).map(node),
    sessions: loose,
  };
}
