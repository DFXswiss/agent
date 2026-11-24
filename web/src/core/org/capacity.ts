// The capacity a device reports to the hub (03-api.md section 4.6.2, ADR-075 section 2) and its check. A module of its
// own so that the monitoring slice of the first load does not pull the device screen's flows (`devices.ts`).
import { isRecord } from '../registry';

export interface Capacity {
  readonly vm_memory_budget_mib: number;
  readonly vm_memory_used_mib: number;
  readonly vm_slots_total: number;
  readonly vm_slots_free: number;
  readonly pool_ready: number;
  // optional counters and settings of a runner that reports them (ADR-075 section 2)
  readonly cpus?: number;
  readonly memory_total_mib?: number;
  readonly disk_free_mib?: number;
  readonly vms_running?: number;
  readonly sessions_active?: number;
  readonly work_vms_running?: number;
  /** Occupying sessions per VM (0 = no limit). */
  readonly sessions_per_vm?: number;
  /** VMs of the device that occupy a slot (0 = only the memory budget decides). */
  readonly vms_max?: number;
}

const CAPACITY_FIELDS = ['vm_memory_budget_mib', 'vm_memory_used_mib', 'vm_slots_total', 'vm_slots_free', 'pool_ready'];

/** A non-negative safe integer. */
export const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

export const isCapacity = (value: unknown): value is Capacity =>
  isRecord(value) && CAPACITY_FIELDS.every((field) => count(value[field]));
