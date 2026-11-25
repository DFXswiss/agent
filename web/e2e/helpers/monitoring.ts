// Monitoring data of the device double (scripts/hub_test_metrics.py, WEB-12): VMs, samples and usage pushed through
// the hub's outbox as the device, rollups written for ranges beyond the raw retention.
import { DIRECTOR_ORIGIN } from '../fixtures/env.ts';

async function post(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${DIRECTOR_ORIGIN}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director POST ${path}: ${JSON.stringify(data)}`);
  return data;
}

/** A VM of the device in the workgroup of `session` (its open sessions are linked to it). */
export async function seedVm(device: string, session: string, state = 'running'): Promise<string> {
  const answer = (await post(`/devices/${device}/vms`, { session_id: session, state })) as { vm_id: string };
  return answer.vm_id;
}

export interface SeedSample {
  readonly entity_kind: 'device' | 'vm' | 'session';
  readonly entity_id: string;
  /** Seconds before now (default 0). */
  readonly age_s?: number;
  readonly [value: string]: unknown;
}

export async function seedSamples(device: string, samples: readonly SeedSample[]): Promise<void> {
  await post(`/devices/${device}/metrics`, { samples });
}

export async function seedUsage(device: string, session: string, fields: Record<string, unknown> = {}): Promise<void> {
  await post(`/devices/${device}/usage`, { session_id: session, ...fields });
}

export interface SeedRollup {
  readonly entity_kind: 'device' | 'vm' | 'session';
  readonly entity_id: string;
  /** Hours before now. */
  readonly age_h: number;
  readonly [value: string]: unknown;
}

export async function seedRollups(device: string, rollups: readonly SeedRollup[]): Promise<void> {
  await post(`/devices/${device}/rollups`, { rollups });
}

/** An outbox audit row of `session` as its device writes it (e.g. `session.model_switched`, ADR-081 section 5). */
export async function seedAudit(
  device: string,
  session: string,
  action: string,
  detail: Record<string, unknown>,
): Promise<void> {
  await post(`/devices/${device}/audit`, { session_id: session, action, detail });
}
