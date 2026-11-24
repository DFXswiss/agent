// Steps of PERF-05 (07-testing.md section 2.2; PFM-01): many workgroups of one org get a platform device through the
// API (the batch of the first-task flow, 03-api.md section 4.6.1 step 1, built with the web core exactly like
// `core/tasks/send.ts` `ensureDevice`), probes of the hub's and the runner's responsiveness while the device starts
// many sessions, and the orchestrator's evaluation routes of the final PERF run (`orchestrator/routes/perf_final.py`).
import { formatTime } from '../../../web/src/core/encoding/time';
import { buildBatch } from '../../../web/src/core/log/rotation';
import { epochKey } from '../../../web/src/core/log/state';
import type { PageUser } from '../../../web/e2e/helpers/user.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';

export interface Summary {
  readonly n: number;
  readonly min: number | null;
  readonly p50: number | null;
  readonly p95: number | null;
  readonly max: number | null;
  readonly limit: number | null;
}

export interface Evaluated {
  readonly summary: Summary;
  readonly passed: boolean;
  readonly csv: string;
}

/** Appends a series to `<artifacts>/perf-<name>.csv` and answers its distribution. */
export async function series(
  orchestrator: Orchestrator,
  name: string,
  label: string,
  values: readonly number[],
  limit?: number,
): Promise<Evaluated> {
  const evaluated = await orchestrator.call<Evaluated>('POST', '/perf/samples', {
    name,
    label,
    values,
    ...(limit !== undefined && { limit }),
  });
  console.log(`${name} ${label}: ${JSON.stringify(evaluated.summary)} passed=${evaluated.passed}`);
  return evaluated;
}

export interface Load {
  readonly hub: string | null;
  readonly runner: string | null;
  readonly vms: {
    running: number;
    running_of_run: number;
    running_foreign: number;
    running_memory_mib: number;
    budget_mib: number;
    vm_memory_mib: number;
  } | null;
}

/** Device load at the time of a measurement (`uptime`, running VMs). */
export async function load(orchestrator: Orchestrator, moment: string): Promise<Load> {
  const found = await orchestrator.call<Load>('GET', '/perf/load');
  console.log(`load ${moment}: ${JSON.stringify(found)}`);
  return found;
}

export interface HubLog {
  readonly hub_lines: number;
  readonly pg_lines: number;
  readonly requests: { requests: number; by_status: Record<string, number>; refused: Record<string, number> };
  readonly deadlocks: number;
}

/** Request statuses of the hub and PostgreSQL deadlocks after a mark taken earlier (or the whole run). */
export const hubLog = (orchestrator: Orchestrator, since?: HubLog) =>
  orchestrator.call<HubLog>('POST', '/perf/hub-log', {
    hub_since: since?.hub_lines ?? 0,
    pg_since: since?.pg_lines ?? 0,
  });

/** Terminal storage at the hub (ADR-016, AUD-01 B-19) of the given sessions. */
export const terminalStorage = (orchestrator: Orchestrator, label: string, sessions: readonly string[]) =>
  orchestrator.call<{ summary: Record<string, unknown>; csv: string }>('POST', '/perf/terminal-storage', {
    label,
    sessions,
  });

/** Milliseconds `action` takes. */
export async function timed(action: () => Promise<unknown>): Promise<number> {
  const started = performance.now();
  await action();
  return performance.now() - started;
}

/** The HTTP status of a failed `PageUser.request` (`<METHOD> <target>: <status> <body>`), else 0. */
export function failedStatus(error: unknown): number {
  const found = error instanceof Error ? /: (\d{3}) /.exec(error.message) : null;
  return found === null ? 0 : Number(found[1]);
}

/** `action`, repeated after `429 rate_limited` (the browser session's 600 requests per minute, 03-api.md section 1). */
export async function paced<T>(action: () => Promise<T>, attempts = 10): Promise<T> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await action();
    } catch (error) {
      if (failedStatus(error) !== 429 || attempt >= attempts) throw error;
      await new Promise((resolve) => setTimeout(resolve, 2_000));
    }
  }
}

interface Candidate {
  readonly device_id: string;
  readonly platform_seq: number;
}

/**
 * Assigns the platform device `device` to `wg`: one `device_assign` batch with the workgroup's and the org's current
 * keys for the device (ADR-007). The app also wraps the user's own `CK` for the device (best effort); the sessions of
 * PERF-05 use the platform's model access and need no user credential, so this step is left out.
 */
export async function assignPlatformDevice(user: PageUser, org: string, wg: string, device: string): Promise<void> {
  const offered = (await user.request('GET', `/v1/workgroups/${wg}/device-candidates`)) as {
    candidates: Candidate[];
  };
  const candidate = offered.candidates.find((item) => item.device_id === device);
  if (candidate === undefined) throw new Error(`device ${device} is no candidate of ${wg}`);
  const log = await user.log(org);
  const keys = await user.keys(org, log);
  const platform = await user.platform();
  const built = await buildBatch({
    log,
    author: { id: user.userId, identity: user.identity },
    entries: [{ type: 'device_assign', device, wg, platform_seq: candidate.platform_seq }],
    keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
    platform,
    ts: formatTime(Date.now()),
  });
  await user.request('POST', `/v1/orgs/${org}/log`, built.body);
}

/**
 * Calls `probe` every `intervalMs` until `stop()` is true; returns the duration of every call in ms and the number of
 * failed calls.
 */
export async function probeWhile(
  probe: () => Promise<unknown>,
  intervalMs: number,
  stop: () => boolean,
): Promise<{ durations: number[]; failures: number }> {
  const durations: number[] = [];
  let failures = 0;
  while (!stop()) {
    const started = performance.now();
    try {
      await probe();
      durations.push(performance.now() - started);
    } catch {
      failures += 1;
    }
    const rest = intervalMs - (performance.now() - started);
    if (rest > 0) await new Promise((resolve) => setTimeout(resolve, rest));
  }
  return { durations, failures };
}
