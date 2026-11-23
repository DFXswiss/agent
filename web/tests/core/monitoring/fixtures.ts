// Overview and sample fixtures of the monitoring tests (shapes of contracts/api/monitoring-overview.response.json).
import type { MetricSample, Usage } from '../../../src/core/monitoring/overview';

export const T0 = Date.parse('2026-11-22T12:00:00.000Z');
export const iso = (ms: number): string => new Date(ms).toISOString();

export const DEV = '01930000-0000-7000-8000-00000000d001';
export const DEV2 = '01930000-0000-7000-8000-00000000d002';
export const WG = '01930000-0000-7000-8000-00000000a001';
export const WG2 = '01930000-0000-7000-8000-00000000a002';
export const TASK = '01930000-0000-7000-8000-00000000c001';
export const S1 = '01930000-0000-7000-8000-00000000e001';
export const S2 = '01930000-0000-7000-8000-00000000e002';

export function sample(ts: number, extra: Partial<MetricSample> = {}): MetricSample {
  return {
    ts: iso(ts),
    cpu_ratio: 0.5,
    mem_used_bytes: 2 * 1024 ** 3,
    mem_limit_bytes: 8 * 1024 ** 3,
    disk_used_bytes: 10 * 1024 ** 3,
    disk_limit_bytes: 100 * 1024 ** 3,
    net_rx_bytes: 1000,
    net_tx_bytes: 500,
    uptime_seconds: 3600,
    reachable: true,
    state: 'ready',
    free_vm_slots: 6,
    free_mem_bytes: 48 * 1024 ** 3,
    ...extra,
  };
}

export const CAPACITY = {
  vm_memory_budget_mib: 65_536,
  vm_memory_used_mib: 16_384,
  vm_slots_total: 8,
  vm_slots_free: 6,
  pool_ready: 1,
};

export function usage(extra: Partial<Usage> = {}): Usage {
  return {
    requests: 3,
    input_tokens: 1200,
    output_tokens: 300,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    thinking_tokens: 0,
    cost_micro_usd: 12_345,
    cost_basis: 'list_price',
    context_used_tokens: 50_000,
    context_window_tokens: 200_000,
    last_provider: 'anthropic',
    updated_at: iso(T0),
    ...extra,
  };
}

/** A hub overview body: device DEV with VMs vm-00000001 (WG) and vm-00000002 (WG2), sessions S1 (WG), S2 (WG2). */
export function overviewBody(extra: Record<string, unknown> = {}) {
  return {
    devices: [
      { device_id: DEV, online: true, capacity: CAPACITY, capacity_low: false, latest: sample(T0), stale: false },
    ],
    vms: [
      { vm_id: 'vm-00000001', device_id: DEV, wg_id: WG, state: 'running', latest: sample(T0), stale: false },
      { vm_id: 'vm-00000002', device_id: DEV, wg_id: WG2, state: 'running', latest: null, stale: true },
    ],
    sessions: [
      { session_id: S1, task_id: TASK, wg_id: WG, state: 'working', latest: sample(T0), usage: usage(), stale: false },
      { session_id: S2, task_id: TASK, wg_id: WG2, state: 'idle', latest: null, usage: null },
    ],
    ...extra,
  };
}

export const ORG = '01930000-0000-7000-8000-0000000000aa';

/** An audit row as `GET /v1/orgs/{org}/audit` lists it. */
export function auditRow(seq: number, at: number, extra: Record<string, unknown> = {}) {
  return {
    seq,
    at: iso(at),
    actor_id: ORG,
    actor_kind: 'user',
    action: 'log.genesis',
    result: 'ok',
    org_id: ORG,
    wg_id: null,
    task_id: null,
    session_id: null,
    device_id: null,
    target_kind: null,
    target_id: null,
    detail: {},
    event_id: null,
    ...extra,
  };
}
