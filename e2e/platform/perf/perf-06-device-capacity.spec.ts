// PERF-06 (07-testing.md section 2.2; KONZEPT E-02, AUD-04 section 6 "Skalierung"; KOR-60): the capacity of one
// execution device, measured with the model double. The load rises in steps until a fixed bound breaks or the device
// has no VM slot left; the capacity is the last step within every bound, and the first bound that broke (or the slot
// budget) names the bottleneck. The number is a capacity statement, not a target: the spec fails only when it could
// not measure (no step at all within the bounds, a session that never started).
//
// Part A - sessions in one workgroup VM: one workgroup, its first VM, then `E2E_PERF_CAPACITY_VM_STEP` further first
// tasks at once per step (each task is its own session in its own sandbox of that VM) up to
// `E2E_PERF_CAPACITY_VM_MAX` sessions.
// Part B - workgroup VMs on the device: after the VMs of part A are stopped (`/perf/quiesce`), steps of
// `E2E_PERF_CAPACITY_STEP` new workgroups with one first task each, all sent at once, up to `E2E_PERF_CAPACITY_MAX`
// VMs or until the device reports fewer free VM slots than the next step needs (05-runner.md section 3.4).
//
// Every session stays alive after its answer (idle Claude Code process in its sandbox), so the load of a step is all
// sessions of the earlier steps plus the new ones. Per step: time to working and time to the first answer of the new
// sessions (hub clock), hub health and `agent-runner status` while the step starts, and the device's, the VMs' and
// the sessions' own samples from the monitoring (hub.metric_sample, every 15 s).
import { expect, test } from '../fixtures/test.ts';
import { runnerCli } from '../fixtures/identity.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { onboard, platformDevice, uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { evaluate, PROMPT } from './measure.ts';
import { assignPlatformDevice, load, paced, probeWhile, series, type Summary } from './parallel.ts';

/** Step sizes and maxima; the defaults measure a device with 256 GiB and 8 GiB VMs in about 40 minutes. */
const VM_STEP = Number(process.env.E2E_PERF_CAPACITY_VM_STEP ?? '4');
const VM_MAX = Number(process.env.E2E_PERF_CAPACITY_VM_MAX ?? '32');
const STEP = Number(process.env.E2E_PERF_CAPACITY_STEP ?? '6');
const MAX = Number(process.env.E2E_PERF_CAPACITY_MAX ?? '60');

/** The fixed bounds of a step (exported to the report as they are). */
export const BOUNDS = {
  /** p95 of `first_working_at - task.created_at` of the step's new sessions. */
  start_p95_s: 30,
  /** p95 of the first `session.output` received by the hub - `task.created_at` (the answer of the double). */
  answer_p95_s: 45,
  /** p95 of `GET /v1/health` from the test process while the step starts. */
  hub_p95_ms: 1_000,
  /** p95 of `agent-runner status --json` on the device while the step starts. */
  runner_p95_ms: 10_000,
  /** Physical memory in use on the device (its own sample) after the step. */
  device_mem_ratio: 0.85,
  /** Memory of the sessions of one VM (their cgroups) relative to the VM's memory (part A). */
  vm_mem_ratio: 0.8,
} as const;

/** Waits for every new session to work and answer; a step that takes longer has failed anyway. */
const STEP_WAIT_MS = 900_000;
/** The runner samples every 15 s; two rounds after the step make sure every new session has its own sample. */
const SAMPLE_WAIT_MS = 60_000;
/** Quiet time before a step: the samples and the hub's request budget settle after the set-up of its workgroups. */
const SETTLE_MS = 15_000;
/** The first task of the run is the double's one ordered step; every further one is answered by a side step (the run
 * stops at a bound, so the number of answers is not known in advance, and an ordered step must be consumed). */
const FIRST_PROMPT = 'What is 1+1?';
const SCENARIO = {
  name: 'perf-06-device-capacity',
  description: 'PERF-06: one answer per task; the number of tasks depends on where the capacity ends.',
  steps: [
    {
      id: 'first',
      expect: { user_text_contains: FIRST_PROMPT, has_tools: true, stream: true },
      reply: { content: [{ type: 'text', text: '2' }] },
    },
  ],
  side: [
    { id: 'title', expect: { has_tools: false }, reply: { content: [{ type: 'text', text: 'Arithmetic' }] } },
    {
      id: 'answer',
      expect: { user_text_contains: PROMPT, has_tools: true, stream: true },
      reply: { content: [{ type: 'text', text: '4' }] },
    },
  ],
};

interface Row {
  readonly task_id: string;
  readonly session_id: string;
  readonly state: string;
  readonly working: boolean;
  readonly answered: boolean;
  readonly vm_id: string | null;
}

interface Sampled {
  readonly entity_id: string;
  readonly cpu_ratio: number | null;
  readonly mem_used_bytes: number | null;
  readonly mem_limit_bytes: number | null;
  readonly free_vm_slots: number | null;
}

export interface Step {
  readonly part: 'sessions-in-one-vm' | 'vms-on-device';
  readonly sessions: number;
  readonly vms: number;
  readonly added: number;
  readonly start_s: Summary;
  readonly answer_s: Summary;
  readonly hub_ms: Summary;
  readonly runner_ms: Summary;
  readonly probe_failures: number;
  readonly failed_sessions: number;
  readonly device: { cpu_ratio: number | null; mem_used_gib: number | null; mem_ratio: number | null } | null;
  readonly free_vm_slots: number | null;
  readonly vm_rss_gib: number | null;
  readonly session_mem_mib: { mean: number | null; max: number | null; sum_gib: number | null };
  readonly vm_mem_ratio: number | null;
  readonly load: string | null;
  readonly broken: string[];
}

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
const round = (value: number | null, digits = 3) =>
  value === null ? null : Math.round(value * 10 ** digits) / 10 ** digits;
const ids = (values: readonly string[]) => values.map((value) => `'${uuid(value)}'`).join(', ');

async function sessionsOf(orchestrator: Orchestrator, tasks: readonly string[]): Promise<Row[]> {
  return orchestrator.hubQuery<Row>(
    `SELECT t.id::text AS task_id, s.id::text AS session_id, s.state, s.first_working_at IS NOT NULL AS working,
            EXISTS (SELECT 1 FROM hub.event e WHERE e.task_id = t.id AND e.kind = 'session.output') AS answered,
            s.vm_id::text AS vm_id
       FROM hub.task t JOIN hub.session s ON s.task_id = t.id AND s.depth = 0
      WHERE t.id IN (${ids(tasks)}) ORDER BY t.created_at`,
  );
}

async function answerSeconds(orchestrator: Orchestrator, tasks: readonly string[]): Promise<number[]> {
  const rows = await orchestrator.hubQuery<{ seconds: number }>(
    `SELECT extract(epoch FROM min(e.received_at) - t.created_at)::float8 AS seconds
       FROM hub.task t JOIN hub.event e ON e.task_id = t.id AND e.kind = 'session.output'
      WHERE t.id IN (${ids(tasks)}) GROUP BY t.id, t.created_at`,
  );
  return rows.map((row) => row.seconds);
}

/** The newest sample of each entity taken after `since` (ISO time of the hub clock). */
async function latest(orchestrator: Orchestrator, kind: string, entities: readonly string[], since: string) {
  if (entities.length === 0) return [];
  return orchestrator.hubQuery<Sampled>(
    `SELECT DISTINCT ON (entity_id) entity_id, cpu_ratio, mem_used_bytes, mem_limit_bytes, free_vm_slots
       FROM hub.metric_sample
      WHERE entity_kind = '${kind}' AND entity_id IN (${entities.map((id) => `'${id}'`).join(', ')})
        AND ts > '${since}'::timestamptz
      ORDER BY entity_id, ts DESC`,
  );
}

const hubNow = async (orchestrator: Orchestrator) =>
  (await orchestrator.hubQuery<{ now: string }>(`SELECT now()::text AS now`))[0]!.now;

test.describe.configure({ mode: 'serial' });

test('@PERF-06 @needs-vm the capacity of one device: sessions per VM and VMs per device within fixed bounds', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'PERF-06 measures the runner device alone (T1, 07-testing.md section 2.2)');
  test.setTimeout(10_800_000);
  await orchestrator.loadScenario('model', SCENARIO);
  const device = await platformDevice(orchestrator);
  await authenticator('platform');
  const { org, recoveryCode } = await onboard(page, orchestrator, 'Perf capacity');
  const user = await PageUser.open(page, recoveryCode);
  const health = async () => {
    const response = await fetch(`http://${e2e.browserHub}/v1/health`);
    if (!response.ok) throw new Error(`health ${response.status}`);
  };
  const runnerStatus = async () => {
    const status = await runnerCli(orchestrator, 'runner-1', ['status', '--json']);
    if (status.rc !== 0) throw new Error(status.stderr);
  };
  const steps: Step[] = [];

  /** One step: send the prompts of every workgroup at once, wait for every session to work and answer, measure. */
  async function step(
    part: Step['part'],
    groups: readonly { wg: string; prompts: readonly string[] }[],
    all: string[],
  ): Promise<Step> {
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    let done = false;
    const hubProbe = probeWhile(health, 3_000, () => done);
    const runnerProbe = probeWhile(runnerStatus, 5_000, () => done);
    let tasks: string[] = [];
    try {
      tasks = (
        await Promise.all(groups.map(({ wg, prompts }) => paced(() => user.createTasks(org, wg, prompts))))
      ).flat();
      all.push(...tasks);
      await expect
        .poll(
          async () => {
            const rows = await sessionsOf(orchestrator, tasks);
            const failed = rows.filter((row) => row.state === 'failed').length;
            return failed > 0 || rows.filter((row) => row.answered).length === tasks.length;
          },
          { timeout: STEP_WAIT_MS, intervals: [2000] },
        )
        .toBe(true);
    } finally {
      done = true;
    }
    const [hub, runner] = await Promise.all([hubProbe, runnerProbe]);
    const since = await hubNow(orchestrator);
    const rows = await sessionsOf(orchestrator, all);
    const vms = [...new Set(rows.map((row) => row.vm_id).filter((vm): vm is string => vm !== null))];
    // Wait for samples taken after the step: the device, every VM and every session of the run.
    let deviceSample: Sampled | undefined;
    let vmSamples: Sampled[];
    let sessionSamples: Sampled[];
    const deadline = Date.now() + SAMPLE_WAIT_MS;
    for (;;) {
      [deviceSample] = await latest(orchestrator, 'device', [uuid(device)], since);
      vmSamples = await latest(orchestrator, 'vm', vms, since);
      sessionSamples = await latest(
        orchestrator,
        'session',
        rows.map((row) => uuid(row.session_id)),
        since,
      );
      const complete =
        deviceSample !== undefined && vmSamples.length === vms.length && sessionSamples.length === rows.length;
      if (complete || Date.now() > deadline) break;
      await new Promise((resolve) => setTimeout(resolve, 3_000));
    }
    const label = `${part} ${rows.length} sessions in ${vms.length} VMs`;
    const start = await evaluate(orchestrator, `PERF-06 ${label}`, tasks, BOUNDS.start_p95_s);
    const startValues = start.runs.map((run) => run.total_s).filter((value): value is number => value !== null);
    const startSeries = await series(orchestrator, 'perf06-start', label, startValues, BOUNDS.start_p95_s);
    const answerSeries = await series(
      orchestrator,
      'perf06-answer',
      label,
      await answerSeconds(orchestrator, tasks),
      BOUNDS.answer_p95_s,
    );
    const hubSeries = await series(orchestrator, 'perf06-hub', label, hub.durations, BOUNDS.hub_p95_ms);
    const runnerSeries = await series(orchestrator, 'perf06-runner', label, runner.durations, BOUNDS.runner_p95_ms);
    const memories = sessionSamples
      .map((sample) => sample.mem_used_bytes)
      .filter((value): value is number => value !== null);
    const sum = memories.reduce((total, value) => total + value, 0);
    const vmLimit = vmSamples.reduce((total, sample) => total + (sample.mem_limit_bytes ?? 0), 0);
    const deviceRatio =
      deviceSample?.mem_used_bytes != null && deviceSample.mem_limit_bytes
        ? deviceSample.mem_used_bytes / deviceSample.mem_limit_bytes
        : null;
    const vmRatio = part === 'sessions-in-one-vm' && vmLimit > 0 ? sum / vmLimit : null;
    const failed = rows.filter((row) => row.state === 'failed').length;
    const broken: string[] = [];
    const p95 = (summary: Summary) => summary.p95 ?? Number.POSITIVE_INFINITY;
    if (failed > 0) broken.push(`failed sessions ${failed}`);
    if (p95(startSeries.summary) > BOUNDS.start_p95_s) broken.push('start_p95_s');
    if (p95(answerSeries.summary) > BOUNDS.answer_p95_s) broken.push('answer_p95_s');
    if (hub.durations.length > 0 && p95(hubSeries.summary) > BOUNDS.hub_p95_ms) broken.push('hub_p95_ms');
    if (runner.durations.length > 0 && p95(runnerSeries.summary) > BOUNDS.runner_p95_ms) broken.push('runner_p95_ms');
    if (hub.failures + runner.failures > 0) broken.push(`probe failures ${hub.failures + runner.failures}`);
    if (deviceRatio !== null && deviceRatio > BOUNDS.device_mem_ratio) broken.push('device_mem_ratio');
    if (vmRatio !== null && vmRatio > BOUNDS.vm_mem_ratio) broken.push('vm_mem_ratio');
    const measured: Step = {
      part,
      sessions: rows.length,
      vms: vms.length,
      added: tasks.length,
      start_s: startSeries.summary,
      answer_s: answerSeries.summary,
      hub_ms: hubSeries.summary,
      runner_ms: runnerSeries.summary,
      probe_failures: hub.failures + runner.failures,
      failed_sessions: failed,
      device:
        deviceSample === undefined
          ? null
          : {
              cpu_ratio: round(deviceSample.cpu_ratio),
              mem_used_gib: round(deviceSample.mem_used_bytes === null ? null : deviceSample.mem_used_bytes / GIB),
              mem_ratio: round(deviceRatio),
            },
      free_vm_slots: deviceSample?.free_vm_slots ?? null,
      vm_rss_gib: round(vmSamples.reduce((total, sample) => total + (sample.mem_used_bytes ?? 0), 0) / GIB),
      session_mem_mib: {
        mean: memories.length === 0 ? null : round(sum / memories.length / MIB, 1),
        max: memories.length === 0 ? null : round(Math.max(...memories) / MIB, 1),
        sum_gib: round(sum / GIB),
      },
      vm_mem_ratio: round(vmRatio),
      load: (await load(orchestrator, label)).runner,
      broken,
    };
    console.log(`PERF-06 step ${JSON.stringify(measured)}`);
    steps.push(measured);
    return measured;
  }

  // Part A: sessions in one workgroup VM.
  const partA: string[] = [];
  const own = await paced(() => user.createWorkgroup(org, 'Capacity one VM'));
  await paced(() => assignPlatformDevice(user, org, own, device));
  let stopA = `reached ${VM_MAX} sessions`;
  const first = await step('sessions-in-one-vm', [{ wg: own, prompts: [FIRST_PROMPT] }], partA);
  if (first.broken.length > 0) stopA = first.broken.join(', ');
  else
    for (let sessions = 1; sessions < VM_MAX;) {
      const add = Math.min(VM_STEP, VM_MAX - sessions);
      const measured = await step('sessions-in-one-vm', [{ wg: own, prompts: Array<string>(add).fill(PROMPT) }], partA);
      sessions = measured.sessions;
      if (measured.vms !== 1) {
        stopA = `the workgroup's sessions spread over ${measured.vms} VMs`;
        break;
      }
      if (measured.broken.length > 0) {
        stopA = measured.broken.join(', ');
        break;
      }
    }

  // Part B: workgroup VMs on the device, from an idle device.
  const quiesced = await orchestrator.call<{ running_before: number; seconds: number }>('POST', '/perf/quiesce', {});
  console.log(`quiesce: ${JSON.stringify(quiesced)}`);
  await platformDevice(orchestrator);
  const partB: string[] = [];
  let stopB = `reached ${MAX} VMs`;
  for (let vms = 0; vms < MAX;) {
    const add = Math.min(STEP, MAX - vms);
    const [capacity] = await orchestrator.hubQuery<{ free: number; total: number }>(
      `SELECT (capacity->>'vm_slots_free')::int AS free, (capacity->>'vm_slots_total')::int AS total
         FROM hub.device WHERE id = '${uuid(device)}'`,
    );
    // The pool VMs count as used slots but take the next first tasks (05-runner.md section 3.4).
    const [pool] = await orchestrator.hubQuery<{ ready: number }>(
      `SELECT coalesce((capacity->>'pool_ready')::int, 0) AS ready FROM hub.device WHERE id = '${uuid(device)}'`,
    );
    if (capacity!.free + pool!.ready < add) {
      stopB = `VM slots: ${capacity!.free} free + ${pool!.ready} pool of ${capacity!.total} for ${add} more VMs`;
      break;
    }
    const groups: { wg: string; prompts: string[] }[] = [];
    for (let n = 1; n <= add; n += 1) {
      const wg = await paced(() => user.createWorkgroup(org, `Capacity VM ${vms + n}`));
      await paced(() => assignPlatformDevice(user, org, wg, device));
      groups.push({ wg, prompts: [PROMPT] });
    }
    const measured = await step('vms-on-device', groups, partB);
    vms = measured.vms;
    if (measured.broken.length > 0) {
      stopB = measured.broken.join(', ');
      break;
    }
  }

  const within = (part: Step['part']) => steps.filter((item) => item.part === part && item.broken.length === 0);
  const capacityA = Math.max(0, ...within('sessions-in-one-vm').map((item) => item.sessions));
  const capacityB = Math.max(0, ...within('vms-on-device').map((item) => item.vms));
  console.log(
    `PERF-06 ${JSON.stringify({
      bounds: BOUNDS,
      steps: { vm_step: VM_STEP, vm_max: VM_MAX, step: STEP, max: MAX },
      sessions_per_vm: { capacity: capacityA, stopped_by: stopA },
      vms_per_device: { capacity: capacityB, stopped_by: stopB },
      table: steps,
    })}`,
  );
  expect(capacityA, `sessions in one VM within the bounds (stopped by ${stopA})`).toBeGreaterThan(0);
  expect(capacityB, `VMs on the device within the bounds (stopped by ${stopB})`).toBeGreaterThan(0);
});
