// PERF-05 (07-testing.md section 2.2; 09-milestones.md WP-M3-03 "30 parallel first tasks"; PFM-01): 30 workgroups with
// one session each on the runner device. The 30 first tasks are sent at once; every session must reach `working` in
// its own VM, and the hub and the runner stay responsive meanwhile: hub health from this process and `agent-runner
// status` answer within their bounds during the start, and PERF-03 holds (event latency runner outbox → browser frame,
// p95 < 300 ms over 1000 events) while all 30 sessions write terminal output (the marker writer of SCN-08 in every
// pane; the browser follows one of the 30 terminals).
//
// Preparation: the workgroup VMs the earlier specs of the iteration left running are stopped first (`/perf/quiesce`),
// then the device's VM memory budget and its free VM slots must hold the 30 VMs (07-testing.md section 7,
// 05-runner.md section 3.4). The warm pool (size 2) is full, so 28 sessions take the clone path at the same time.
//
// Also measured here (AUD-01, PFM-01): A-30 – three first tasks that empty the pool and then need a clone within two
// seconds: the third one's estimate must already be the clone path's (the runner's early heartbeat), and the
// estimates of the burst of 30 are recorded against the path each session took; B-20 – membership appends
// (`POST /v1/orgs/{org}/log`, the org row `FOR UPDATE`) idle and while 30 sessions push terminal events into the same
// org (`FOR SHARE` on that row), then all 30 sessions are stopped at once while 29 of them still write, with the hub's
// refusals (429/5xx) and PostgreSQL's deadlocks of the window; B-19 – the terminal storage of the 30 sessions.
import { expect, test } from '../fixtures/test.ts';
import { navigate, runnerCli } from '../fixtures/identity.ts';
import { onboard, platformDevice, uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { CLONE_ESTIMATE_MS, evaluate, PROMPT, scenario } from './measure.ts';
import {
  EVENTS,
  type Evaluation,
  framesSince,
  MARK_FACTOR,
  MARK_INTERVAL_MS,
  report,
  startMarks,
  tapStream,
} from './latency.ts';
import {
  assignPlatformDevice,
  failedStatus,
  hubLog,
  load,
  paced,
  probeWhile,
  series,
  terminalStorage,
  timed,
} from './parallel.ts';

/** Workgroups with one session each (07-testing.md section 2.2); `E2E_PERF_PARALLEL` only for trial runs. */
const PARALLEL = Number(process.env.E2E_PERF_PARALLEL ?? '30');
/** Workgroups of the A-30 probe: two empty the pool of 2, the third needs a clone. */
const PROBE = 3;
/** Membership appends idle and under load (B-20). */
const APPENDS = 10;
/** Responsiveness bounds (p95 and maximum): hub health and the runner's CLI status. */
const HUB_PROBE_LIMIT_MS = 1_000;
const RUNNER_PROBE_LIMIT_MS = 10_000;
/**
 * PERF-05 has no time target (07-testing.md section 2.2: "all reach `working`"); the spec asserts this bound for every
 * session's time to working (NB-18, decision PFM-01 9): 30 sessions on one device must all work within five minutes.
 * The wait for all 30 is longer (`PARALLEL_WAIT_MS`), so a slow start fails on the bound with the per-session data.
 */
const PARALLEL_CEILING_S = 300;
const PARALLEL_WAIT_MS = 900_000;
/** A token bucket of the hub refills from empty to full in one minute (03-api.md section 1). */
const BUDGET_REFILL_MS = 60_000;

interface Started {
  readonly task_id: string;
  readonly session_id: string;
  readonly state: string;
  readonly working: boolean;
  readonly vm_id: string | null;
  readonly estimate_ms: number | null;
}

interface Capacity {
  readonly vm_slots_free: number;
  readonly pool_ready: number;
}

test.describe.configure({ mode: 'serial' });

test('@PERF-05 @needs-vm 30 workgroups start one session each at once: all work, hub and runner stay responsive', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'PERF-05 measures the runner device alone (T1, 07-testing.md section 2.2)');
  test.setTimeout(3_600_000);
  await tapStream(page);
  await orchestrator.loadScenario('model', scenario(PARALLEL + PROBE));
  const device = await platformDevice(orchestrator);
  const quiesced = await orchestrator.call<{ running_before: number; seconds: number }>('POST', '/perf/quiesce', {});
  console.log(`quiesce: ${JSON.stringify(quiesced)}`);
  await platformDevice(orchestrator);
  const capacity = async () =>
    (
      await orchestrator.hubQuery<Capacity>(
        `SELECT (capacity->>'vm_slots_free')::int AS vm_slots_free, (capacity->>'pool_ready')::int AS pool_ready
           FROM hub.device WHERE id = '${uuid(device)}'`,
      )
    )[0]!;
  const runnerName = 'runner-1';
  const poolFull = async () => {
    await expect
      .poll(async () => (await orchestrator.pool())[runnerName]?.ready ?? 0, { timeout: 300_000, intervals: [1000] })
      .toBe(2);
    await expect.poll(async () => (await capacity()).pool_ready, { timeout: 60_000, intervals: [500] }).toBe(2);
  };

  await authenticator('platform');
  const { org, recoveryCode } = await onboard(page, orchestrator, 'Perf parallel');
  const user = await PageUser.open(page, recoveryCode);
  const workgroups: string[] = [];
  for (let n = 1; n <= PROBE + PARALLEL; n += 1)
    workgroups.push(await paced(() => user.createWorkgroup(org, `Parallel workgroup ${n}`)));
  for (const wg of workgroups) await paced(() => assignPlatformDevice(user, org, wg, device));
  // The set-up of 33 workgroups spends the browser session's request budget (600 per minute, 03-api.md section 1):
  // let it refill before the measured tasks, so neither the A-30 probe nor the burst meets `429`.
  await new Promise((resolve) => setTimeout(resolve, BUDGET_REFILL_MS));
  const probeGroups = workgroups.slice(0, PROBE);
  const burstGroups = workgroups.slice(PROBE);
  const sessionsOf = async (tasks: readonly string[]) =>
    orchestrator.hubQuery<Started>(
      `SELECT t.id::text AS task_id, s.id::text AS session_id, s.state, s.first_working_at IS NOT NULL AS working,
              s.vm_id::text AS vm_id, s.estimate_ms
         FROM hub.task t JOIN hub.session s ON s.task_id = t.id AND s.depth = 0
        WHERE t.id IN (${tasks.map((task) => `'${uuid(task)}'`).join(', ')}) ORDER BY t.created_at`,
    );

  // A-30: two tasks take both pool VMs; once the runner reports the pool empty, a third task within 2 s.
  await poolFull();
  const probeTasks: string[] = [];
  for (const wg of probeGroups.slice(0, 2))
    probeTasks.push((await paced(() => user.createTasks(org, wg, [PROMPT])))[0]!);
  await expect
    .poll(async () => (await orchestrator.pool())[runnerName]?.ready ?? -1, { timeout: 30_000, intervals: [100] })
    .toBe(0);
  const emptied = performance.now();
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  probeTasks.push((await user.createTasks(org, probeGroups[2]!, [PROMPT]))[0]!);
  const probeAfterMs = performance.now() - emptied;
  await expect
    .poll(async () => (await sessionsOf(probeTasks)).filter((row) => row.working).length, { timeout: 180_000 })
    .toBe(PROBE);
  const probeEstimates = (await sessionsOf(probeTasks)).map((row) => row.estimate_ms);
  console.log(
    `A-30 probe: third task ${Math.round(probeAfterMs)} ms after the pool emptied, estimates ${probeEstimates}`,
  );
  expect(probeEstimates[2], 'estimate of a first task after the pool emptied').toBe(CLONE_ESTIMATE_MS);
  await evaluate(orchestrator, 'A-30 pool emptied', probeTasks, PARALLEL_CEILING_S);

  // Budget and slots for the burst.
  await poolFull();
  const before = await load(orchestrator, 'before the PERF-05 burst');
  expect(before.vms, 'Lima on the runner device').not.toBeNull();
  const vms = before.vms!;
  expect(
    vms.running_memory_mib + (PARALLEL - 2) * vms.vm_memory_mib,
    'running VM memory plus the burst within the VM memory budget',
  ).toBeLessThanOrEqual(vms.budget_mib);
  expect((await capacity()).vm_slots_free, 'free VM slots for the burst').toBeGreaterThanOrEqual(PARALLEL - 2);

  // B-20 baseline: membership appends and hub health on the idle org.
  const idleAppends: number[] = [];
  for (let n = 1; n <= APPENDS; n += 1)
    idleAppends.push(await timed(() => paced(() => user.createWorkgroup(org, `Idle append ${n}`))));
  const health = async () => {
    const response = await fetch(`http://${e2e.browserHub}/v1/health`);
    if (!response.ok) throw new Error(`health ${response.status}`);
  };
  const idleHealth: number[] = [];
  for (let n = 1; n <= 5; n += 1) {
    idleHealth.push(await timed(health));
    await new Promise((resolve) => setTimeout(resolve, 3_000));
  }
  await new Promise((resolve) => setTimeout(resolve, BUDGET_REFILL_MS)); // refill the request budgets of the hub
  const mark = await hubLog(orchestrator);

  // The burst: all first tasks at once (through the page's connections), probes and appends while they start.
  let done = false;
  // Hub health every 3 s stays within the anonymous limit of 30 per minute and source (03-api.md section 1).
  const hubProbe = probeWhile(health, 3_000, () => done);
  const runnerProbe = probeWhile(
    async () => {
      const status = await runnerCli(orchestrator, runnerName, ['status', '--json']);
      if (status.rc !== 0) throw new Error(status.stderr);
    },
    5_000,
    () => done,
  );
  const loadAppends: number[] = [];
  let appendFailures = 0;
  let appendRetries = 0;
  try {
    const sent = performance.now();
    const tasks = (await Promise.all(burstGroups.map((wg) => user.createTasks(org, wg, [PROMPT])))).map(
      (ids) => ids[0]!,
    );
    const sentMs = performance.now() - sent;
    await expect
      .poll(async () => (await sessionsOf(tasks)).filter((row) => row.working).length, {
        timeout: PARALLEL_WAIT_MS,
        intervals: [2000],
      })
      .toBe(PARALLEL);
    const allWorkingMs = performance.now() - sent;
    const during = await load(orchestrator, 'all PERF-05 sessions working');
    done = true;
    const [hub, runnerStatus] = await Promise.all([hubProbe, runnerProbe]);
    const rows = await sessionsOf(tasks);
    const distinctVms = new Set(rows.map((row) => row.vm_id).filter((vm) => vm !== null)).size;

    // The times to working (hub clock; breakdown per transition and start estimate in the CSV).
    const timing = await evaluate(orchestrator, 'PERF-05 parallel', tasks, PARALLEL_CEILING_S);
    expect(timing.summary.runs).toBe(PARALLEL);
    expect(timing.summary.passed, `every PERF-05 session working within ${PARALLEL_CEILING_S} s`).toBe(true);

    // PERF-03 while all 30 sessions write terminal output: the browser follows the first session's terminal; the
    // other 29 write half as long again, so they still push when all sessions are stopped below.
    const watched = rows[0]!;
    await navigate(page, `/o/${org}/t/${watched.task_id}`);
    await page.getByTestId('task.tabs.sessions').click();
    await page.getByTestId('sessions.link').first().click();
    await expect(page.getByTestId('terminal')).toHaveAttribute('data-mode', 'follow', { timeout: 30_000 });
    const since = Date.now();
    const writing = await startMarks(orchestrator, watched.session_id, EVENTS);
    for (const row of rows.slice(1))
      await orchestrator.call('POST', '/latency/marks', {
        session: row.session_id,
        count: Math.ceil(EVENTS * MARK_FACTOR * 1.5),
        interval_ms: MARK_INTERVAL_MS,
      });
    const marksLoad = await load(orchestrator, 'all PERF-05 sessions writing');
    for (let n = 1; n <= APPENDS; n += 1) {
      for (let attempt = 1; ; attempt += 1) {
        try {
          loadAppends.push(await timed(() => user.createWorkgroup(org, `Load append ${n}`)));
          break;
        } catch (error) {
          const status = failedStatus(error);
          console.log(`append under load: ${status}`);
          if (status !== 429 || attempt >= 10) {
            appendFailures += 1;
            break;
          }
          appendRetries += 1;
          await new Promise((resolve) => setTimeout(resolve, 2_000));
        }
      }
    }
    await expect
      .poll(async () => (await framesSince(page, since)).length, { timeout: writing + 300_000, intervals: [2000] })
      .toBeGreaterThanOrEqual(EVENTS);
    await page.waitForTimeout(Math.max(0, since + writing - Date.now()) + 3000);
    const latency = await orchestrator.call<Evaluation>('POST', '/latency/events', {
      label: `PERF-03 during PERF-05 (${PARALLEL} sessions writing)`,
      since_ms: since,
      frames: await framesSince(page, since),
    });
    report('PERF-03 during PERF-05', latency);

    // Stop all sessions at once while 29 still write (audit-lock pattern of ADR-030, B-20).
    const stops = await Promise.all(
      rows.map(async (row) => {
        try {
          await user.request('POST', `/v1/sessions/${row.session_id}/control`, { action: 'stop' });
          return 200;
        } catch (error) {
          return failedStatus(error);
        }
      }),
    );
    await expect
      .poll(async () => (await sessionsOf(tasks)).filter((row) => row.state === 'stopped').length, {
        timeout: 300_000,
        intervals: [2000],
      })
      .toBe(PARALLEL);
    const window = await hubLog(orchestrator, mark);
    const storage = await terminalStorage(
      orchestrator,
      'PERF-05',
      rows.map((row) => row.session_id),
    );

    const label = (name: string) => `${name} (${PARALLEL} parallel)`;
    await series(orchestrator, 'perf05-appends', label('membership append idle'), idleAppends);
    await series(orchestrator, 'perf05-appends', label('membership append while 30 sessions write'), loadAppends);
    await series(orchestrator, 'perf05-responsiveness', label('hub health idle'), idleHealth);
    const hubSeries = await series(
      orchestrator,
      'perf05-responsiveness',
      label('hub health'),
      hub.durations,
      HUB_PROBE_LIMIT_MS,
    );
    const runnerSeries = await series(
      orchestrator,
      'perf05-responsiveness',
      label('runner status'),
      runnerStatus.durations,
      RUNNER_PROBE_LIMIT_MS,
    );
    const count = (values: readonly (number | null)[]) =>
      values.reduce<Record<string, number>>((counts, value) => {
        counts[String(value)] = (counts[String(value)] ?? 0) + 1;
        return counts;
      }, {});
    console.log(
      `PERF-05 ${JSON.stringify({
        parallel: PARALLEL,
        sent_ms: Math.round(sentMs),
        all_working_ms: Math.round(allWorkingMs),
        distinct_vms: distinctVms,
        time_to_working: timing.summary,
        estimates: count(rows.map((row) => row.estimate_ms)),
        appends: { idle: idleAppends.length, while_writing: loadAppends.length, retried_429: appendRetries },
        probe_failures: { hub: hub.failures, runner: runnerStatus.failures },
        stops: count(stops),
        perf03: latency.summary,
        marks_load: marksLoad,
        window: { requests: window.requests, deadlocks: window.deadlocks },
        storage: storage.summary,
        load: { before, during },
      })}`,
    );
    // Soft: every criterion of PERF-05 is judged in a run, also after an earlier one failed (review of PFM-01).
    expect.soft(distinctVms, 'one VM per workgroup').toBe(PARALLEL);
    expect.soft(latency.summary.samples, 'PERF-03 samples during PERF-05').toBeGreaterThanOrEqual(EVENTS);
    expect.soft(latency.summary.passed, `PERF-03 holds: ${JSON.stringify(latency.summary)}`).toBe(true);
    expect.soft(appendFailures, 'membership appends under load (other than 429)').toBe(0);
    expect.soft(hub.failures + runnerStatus.failures, 'failed probes').toBe(0);
    expect.soft(hubSeries.passed, 'hub responsiveness').toBe(true);
    expect.soft(runnerSeries.passed, 'runner responsiveness').toBe(true);
  } finally {
    done = true;
    await Promise.allSettled([hubProbe, runnerProbe]);
  }
});
