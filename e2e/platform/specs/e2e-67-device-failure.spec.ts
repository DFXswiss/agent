// E2E-67 (KOR-58, ADR-087; 03-api.md section 4.6.5): a device failure interrupts a cloud session, which continues on
// another device. Short deadlines (`POST /hub/failover`: online window 30 s, failure deadline 60 s; the runners send a
// heartbeat every 15 s) instead of 5 minutes.
// 1. Two devices: two further runner instances, A and B, certified platform devices of a workgroup of their own. The
//    task works on A (B is stopped while the task is placed, so the choice is A): a first turn ends idle with a
//    checkpoint, the second instruction starts a long `test.run` on A. A is killed hard (SIGKILL to its process group,
//    `POST /runners/{name}/hard-kill`; its VMs stay), B runs. Before the deadline nothing moves; a prompt sent meanwhile
//    waits for the device. After the deadline the hub marks the session interrupted, ends it `stopped/device_failed`,
//    ends its test run `failed/device_failed` and starts the successor on B (`failover_of`): the same Claude Code
//    conversation (`--resume <id>`, the first turn in the model's request, the failover notice naming the cancelled
//    run), the interrupted instruction first (the session repeats the test run in a fresh VM on B), then the waiting
//    prompt. A comes back (restart with the same home): it stops the orphan session, its late reports change nothing
//    at the hub (no transition, no operation of the lost session after its end), and the run it executed keeps its
//    record and gets its log. Measured: the interruption from the kill to the successor's first `working`.
// 2. One own device: an org device that is the only device of its workgroup is killed while its session works; the
//    task waits `scheduling/device_failed` (attention `blocked/device_failed`, the notice on the task) and nothing
//    moves. The runner comes back with its VMs: the same session goes on (the mark cleared, audit
//    `session.interruption_ended`). Killed again during the next turn and its VMs deleted before it starts (a reboot):
//    it reports the session failed (`device_shutdown`/`guest_unreachable`) and the hub starts the successor on the same
//    device (`failover_of`), which continues the conversation with the interrupted instruction.
// Runs on T1 (runner instances on the runner device, work VMs in self mode).
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, runnerStore, sessionState, sessionVm } from '../fixtures/guest.ts';
import {
  addDeviceInvitation,
  anonymousBudget,
  closingChecks,
  COST,
  enrollRunner,
  expectDevice,
  navigate,
  removeRunner,
  spawnRunner,
} from '../fixtures/identity.ts';
import { storeApiKey, testApiKey } from '../fixtures/model.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { expectAnswerInChat, invite, onboard, operator, uuid } from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import type { Page } from '@playwright/test';

const load = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));
const TWO = load('e2e-67-device-failure');
const ONE = load('e2e-67-single-device');

const ONLINE_SECONDS = 30;
const FAILOVER_SECONDS = 60;
const SWEEP_SECONDS = 15; // job `failover.sweep`
const WG_NAME = 'Failover kz65-wg-f4d7';
const SOLO_WG_NAME = 'Solo kz65-wg-s2b9';
const WORK = 'E2E65-WORK kz65-prompt-w3k8: start the failover notes in notes.md';
const LONG = 'E2E65-LONG kz65-prompt-l5t2: run the long test suite';
const QUEUED = 'E2E65-QUEUED kz65-prompt-q7v4: then summarise the notes';
const WORK_ANSWER = 'The notes are written kz65-answer-w2p9e7.';
const REPEATED = 'The repeated tests passed kz65-answer-r4d8m1.';
const QUEUED_ANSWER = 'Summary after the failover kz65-answer-q3s5k9.';
const FILE_CANARY = 'kz65-file-n6c1x4';
const SOLO_WORK = 'E2E65-SOLO-WORK kz65-prompt-s8w1: start the solo notes';
const SOLO_LONG = 'E2E65-SOLO-LONG kz65-prompt-s4l6: think about the solo notes for a while';
const SOLO_ANSWER = 'The solo notes are written kz65-solo-answer-t6y1.';
const SOLO_THIRD = 'E2E65-SOLO-THIRD kz65-prompt-s9t3: one more solo thought';
const SOLO_LONG_ANSWER = 'The long solo thought is done kz65-solo-answer-l2d6.';
const SOLO_CONTINUED = 'Continued on the same device kz65-solo-answer-c3n7.';
const CHECKPOINT_TIMEOUT = 240_000;
const NOTICE = 'agent.dfx.swiss session notice: ';
const FAILOVER_NOTICE = 'because the device it ran on failed';
const CANCELLED_NOTICE = '1 test run that was running there was cancelled';

interface JournalRecord {
  readonly path: string;
  readonly step?: string | null;
  readonly body: unknown;
}

interface HubSession {
  readonly id: string;
  readonly device_id: string | null;
  readonly state: string;
  readonly state_reason: string | null;
  readonly interrupted: boolean;
  readonly failover_of: string | null;
  readonly working: boolean;
}

interface HubOperation {
  readonly id: string;
  readonly session_id: string;
  readonly state: string;
  readonly error_code: string | null;
}

/** The Claude Code session ID `runner` recorded for `session` (null until the guest reported it). */
async function claudeId(orchestrator: Orchestrator, session: string, runner: string): Promise<string | null> {
  const rows = await runnerStore<{ claude_session_id: string | null }>(
    orchestrator,
    `SELECT claude_session_id FROM runner.session WHERE id = '${uuid(session)}'`,
    runner,
  );
  return rows[0]?.claude_session_id ?? null;
}

/** Waits until `runner` has nothing due for `session` and its checkpoints are committed at the hub (ADR-072). */
async function checkpointed(orchestrator: Orchestrator, task: string, session: string, runner: string): Promise<void> {
  await expect
    .poll(
      async () => {
        const [local] = await runnerStore<{ due: boolean; open: number }>(
          orchestrator,
          `SELECT (SELECT count(*) FROM runner.context_session WHERE session_id = '${uuid(session)}'
                     AND due_at IS NOT NULL) > 0 AS due,
                  (SELECT count(*)::int FROM runner.context_object WHERE session_id = '${uuid(session)}'
                     AND state <> 'committed') AS open`,
          runner,
        );
        const [hub] = await orchestrator.hubQuery<{ n: number }>(
          `SELECT count(*)::int AS n FROM hub.context_object WHERE task_id = '${uuid(task)}' AND state = 'complete'
              AND kind = 'context'`,
        );
        return !local!.due && local!.open === 0 && hub!.n >= 1;
      },
      { timeout: CHECKPOINT_TIMEOUT, intervals: [2000] },
    )
    .toBe(true);
}

/** The newest request the model double answered with step `step` (the whole request body). */
async function requestOf(orchestrator: Orchestrator, step: string): Promise<string> {
  const records = await journal(orchestrator);
  const found = records.filter((record) => record.path === '/v1/messages' && record.step === step).at(-1);
  expect(found, `no request answered by step ${step}`).toBeDefined();
  return JSON.stringify(found!.body);
}

async function journal(orchestrator: Orchestrator): Promise<JournalRecord[]> {
  const { body } = await orchestrator.doubleRequest('model', 'GET', '/_double/journal');
  return (Array.isArray(body) ? body : []) as JournalRecord[];
}

async function consumed(orchestrator: Orchestrator): Promise<string[]> {
  return ((await orchestrator.doubleState('model')).consumed ?? []) as string[];
}

async function sessionsOf(orchestrator: Orchestrator, task: string): Promise<HubSession[]> {
  return orchestrator.hubQuery<HubSession>(
    `SELECT id, device_id::text, state, state_reason, (interrupted_at IS NOT NULL AND (interruption_ended_at IS NULL OR interruption_ended_at < interrupted_at)) AS interrupted,
            failover_of::text, first_working_at IS NOT NULL AS working
       FROM hub.session WHERE task_id = '${uuid(task)}' AND depth = 0 ORDER BY created_at, id`,
  );
}

async function testRuns(orchestrator: Orchestrator, task: string): Promise<HubOperation[]> {
  return orchestrator.hubQuery<HubOperation>(
    `SELECT id::text, session_id::text, state, error_code FROM hub.operation
      WHERE task_id = '${uuid(task)}' AND type = 'test.run' ORDER BY created_at, id`,
  );
}

/** The task as its owner reads it through the browser API (`GET /v1/tasks/{task}`). */
async function taskOf(user: PageUser, task: string): Promise<{ state: string; state_reason: string | null }> {
  const answer = (await user.request('GET', `/v1/tasks/${task}`)) as {
    task: { state: string; state_reason: string | null };
  };
  return { state: answer.task.state, state_reason: answer.task.state_reason };
}

/** The hub's clock now (every duration of this spec is measured on it, never on a device clock). */
async function hubNow(orchestrator: Orchestrator): Promise<string> {
  const [row] = await orchestrator.hubQuery<{ at: string }>(`SELECT clock_timestamp()::text AS at`);
  expect(row!.at).toMatch(/^[0-9 :.+-]+$/);
  return row!.at;
}

/** Milliseconds from `since` (hub time) to the first `working` of `session`, null while it never worked. */
async function workingAfter(orchestrator: Orchestrator, session: string, since: string): Promise<number | null> {
  const [row] = await orchestrator.hubQuery<{ ms: number | null }>(
    `SELECT (extract(epoch FROM first_working_at - '${since}'::timestamptz) * 1000)::float8 AS ms
       FROM hub.session WHERE id = '${uuid(session)}'`,
  );
  return row?.ms ?? null;
}

/** Milliseconds on the hub clock since `since`. */
async function sinceMs(orchestrator: Orchestrator, since: string): Promise<number> {
  const [row] = await orchestrator.hubQuery<{ ms: number }>(
    `SELECT (extract(epoch FROM clock_timestamp() - '${since}'::timestamptz) * 1000)::float8 AS ms`,
  );
  return row!.ms;
}

async function followUp(page: Page, text: string): Promise<void> {
  await page.getByTestId('chat.composer.prompt').fill(text);
  await page.getByTestId('chat.composer.send').click();
}

/** Waits until the runner instance's warm pool holds a VM and the hub has its capacity report saying so. */
async function poolOf(orchestrator: Orchestrator, runner: string, device: string): Promise<void> {
  await expect
    .poll(async () => (await orchestrator.pool())[runner]?.ready ?? 0, { timeout: 300_000, intervals: [1000] })
    .toBeGreaterThanOrEqual(1);
  await expect
    .poll(
      async () =>
        (
          await orchestrator.hubQuery<{ ready: number | null }>(
            `SELECT (capacity->>'pool_ready')::int AS ready FROM hub.device WHERE id = '${uuid(device)}'`,
          )
        )[0]?.ready ?? 0,
      { timeout: 120_000, intervals: [500] },
    )
    .toBeGreaterThanOrEqual(1);
}

/** A further platform device: operator invitation, enrollment, certification, connected. */
async function platformRunner(orchestrator: Orchestrator, name: string): Promise<string> {
  const device = await enrollRunner(orchestrator, name, (await invite(orchestrator, 'device')).code, true);
  expect(await operator(orchestrator, ['certify-pending', '--device', device])).toContain(`certified ${device}`);
  await expectDevice(orchestrator, device, { state: 'active', online: true });
  return device;
}

/** Assigns a platform device to `wg` through the devices screen's dialog (the candidate verified in the browser). */
async function assign(page: Page, org: string, wg: string, device: string): Promise<void> {
  await navigate(page, `/o/${org}/devices`);
  await page.getByTestId('devices.assign.open').click();
  await page.getByTestId('devices.assign.scope').selectOption(wg);
  const candidate = page.locator(`[data-testid="devices.assign.candidate"][data-device="${device}"]`);
  await expect(candidate).toHaveAttribute('data-verified', 'true', { timeout: 60_000 });
  await candidate.getByRole('radio').check();
  await page.getByTestId('devices.assign.confirm').click();
  await expect(page.getByTestId('devices.assign')).toHaveCount(0, { timeout: 60_000 });
}

test.describe.configure({ mode: 'serial' });

test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-67 runs on T1 (runner instances on the runner device)');
});

test('@E2E-67 @R-14.6 @needs-vm @needs-workvm a session of a failed device continues on another device with the same conversation, the interrupted instruction and the waiting prompt; the device that comes back changes nothing', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(2_700_000);
  const spawned: string[] = [];
  try {
    await orchestrator.setFailover(ONLINE_SECONDS, FAILOVER_SECONDS);
    await orchestrator.loadScenario('model', TWO);
    const A = (await spawnRunner(orchestrator, { pool_size: 1 })).name;
    spawned.push(A);
    const B = (await spawnRunner(orchestrator, { pool_size: 1 })).name;
    spawned.push(B);
    // test runs in fresh VMs on both devices (cold clones: no work VM pool)
    await orchestrator.call('POST', '/runners/workvm', { enable: true, pool_size: 0 });
    const deviceA = await platformRunner(orchestrator, A);
    const deviceB = await platformRunner(orchestrator, B);

    await authenticator('platform');
    await anonymousBudget(COST.register);
    const onboarded = await onboard(page, orchestrator, 'Ada kz65-name-a8f3');
    const org = onboarded.org;
    const user = await PageUser.open(page, onboarded.recoveryCode);
    const wg = await user.createWorkgroup(org, WG_NAME);
    await assign(page, org, wg, deviceA);
    await assign(page, org, wg, deviceB);
    const log = await user.log(org);
    expect([log.state.assigned(deviceA, wg), log.state.assigned(deviceB, wg)]).toEqual([true, true]);
    await poolOf(orchestrator, A, deviceA);
    await poolOf(orchestrator, B, deviceB);

    // The task works on A: B is offline while the task is placed.
    await orchestrator.stopRunner(B);
    await expectDevice(orchestrator, deviceB, { online: false });
    const [task] = await user.createTasks(org, wg, [WORK]);
    await navigate(page, `/o/${org}/t/${task!}`);
    await expectAnswerInChat(page, WORK_ANSWER);
    const [lost] = await sessionsOf(orchestrator, task!);
    expect(lost!.device_id).toBe(deviceA);
    await expect.poll(() => claudeId(orchestrator, lost!.id, A), { timeout: 60_000 }).toMatch(/^[0-9a-f-]{36}$/);
    const claude = (await claudeId(orchestrator, lost!.id, A))!;
    await expect.poll(() => sessionState(orchestrator, lost!.id), { timeout: 120_000 }).toBe('idle');
    await checkpointed(orchestrator, task!, lost!.id, A);

    // The second instruction runs a long test on A; its pending result is held by the model double.
    await followUp(page, LONG);
    await expect.poll(() => consumed(orchestrator), { timeout: 180_000, intervals: [1000] }).toContain('poll-a');
    const [runA] = await testRuns(orchestrator, task!);
    expect(runA!.session_id).toBe(lost!.id);
    await expect
      .poll(
        async () =>
          (
            await runnerStore<{ state: string }>(
              orchestrator,
              `SELECT state FROM runner.test_run WHERE operation_id = '${uuid(runA!.id)}'`,
              A,
            )
          )[0]?.state,
        { timeout: 300_000, intervals: [2000] },
      )
      .toBe('running');
    expect(await sessionState(orchestrator, lost!.id)).toBe('working');

    // B is back; A fails hard.
    await orchestrator.startRunner(B);
    await expectDevice(orchestrator, deviceB, { online: true });
    const killedAt = await hubNow(orchestrator);
    const killed = await orchestrator.killRunner(A);
    expect(killed).toMatchObject({ exit: -9, state: 'stopped' });
    await expectDevice(orchestrator, deviceA, { online: false }, 60_000);

    // Before the deadline nothing moves; a prompt sent meanwhile waits for the device.
    await followUp(page, QUEUED);
    await expect(page.getByTestId('chat.queue.item')).toHaveAttribute('data-state', 'waiting_for_device', {
      timeout: 60_000,
    });
    const early = await sessionsOf(orchestrator, task!);
    expect(early.map((row) => [row.id, row.interrupted])).toEqual([[lost!.id, false]]);

    // After the deadline: interrupted, ended `stopped/device_failed`, the successor on B.
    await expect
      .poll(async () => (await sessionsOf(orchestrator, task!)).length, {
        timeout: (FAILOVER_SECONDS + 4 * SWEEP_SECONDS) * 1000 + 60_000,
        intervals: [1000],
      })
      .toBe(2);
    const [ended, successor] = await sessionsOf(orchestrator, task!);
    expect(ended).toMatchObject({ id: lost!.id, state: 'stopped', state_reason: 'device_failed', interrupted: true });
    expect(successor).toMatchObject({ device_id: deviceB, failover_of: lost!.id });
    const [first] = await orchestrator.hubQuery<{ state: string; reason: string | null }>(
      `SELECT state, reason FROM hub.session_transition WHERE session_id = '${uuid(successor!.id)}' ORDER BY seq LIMIT 1`,
    );
    expect(first).toEqual({ state: 'requested', reason: 'device_failed' });
    const interrupted = await auditRows(orchestrator, task!, 'session.interrupted');
    expect(interrupted.map((row) => row.session_id)).toEqual([lost!.id]);
    const [deadline] = await orchestrator.hubQuery<{ seconds: number }>(
      `SELECT extract(epoch FROM interrupted_at - '${killedAt}'::timestamptz)::float8 AS seconds
         FROM hub.session WHERE id = '${uuid(lost!.id)}'`,
    );
    expect(deadline!.seconds).toBeGreaterThanOrEqual(FAILOVER_SECONDS - 1);
    // The run on A was cancelled by the failure; it keeps its record.
    expect((await testRuns(orchestrator, task!))[0]).toMatchObject({
      id: runA!.id,
      state: 'failed',
      error_code: 'device_failed',
    });

    // The interruption: from the kill to the successor's first `working`.
    await expect
      .poll(() => workingAfter(orchestrator, successor!.id, killedAt), { timeout: 300_000, intervals: [1000] })
      .not.toBeNull();
    const interruption = Math.round((await workingAfter(orchestrator, successor!.id, killedAt))!);
    console.log(
      `KOR58 interruption_ms=${interruption} (kill to the successor's first working; deadline ${FAILOVER_SECONDS} s, sweep every ${SWEEP_SECONDS} s)`,
    );

    // A comes back with the same home: the orphan session is stopped there, its late reports change nothing.
    await orchestrator.startRunner(A);
    await expectDevice(orchestrator, deviceA, { online: true });

    // The same conversation on B: the interrupted instruction first (the test run repeated in a fresh VM there),
    // then the prompt that waited.
    await expectAnswerInChat(page, REPEATED);
    await expectAnswerInChat(page, QUEUED_ANSWER);
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const steps = await consumed(orchestrator);
    expect(steps.slice(steps.indexOf('poll-a'))).toEqual(['poll-a', 'again', 'again-wait', 'again-done', 'queued']);
    await expect.poll(() => claudeId(orchestrator, successor!.id, B), { timeout: 60_000 }).toBe(claude);
    const running = await sessionVm(orchestrator, successor!.id);
    expect(running.command.indexOf('--resume'), running.command.join(' ')).toBeGreaterThan(0);
    expect(running.command[running.command.indexOf('--resume') + 1]).toBe(claude);
    const again = await requestOf(orchestrator, 'again');
    for (const earlier of [WORK, 'I will keep the notes first.', FILE_CANARY, WORK_ANSWER, LONG]) {
      expect(again, `the resumed request lacks ${earlier}`).toContain(earlier);
    }
    expect(again).toContain(NOTICE);
    expect(again).toContain(FAILOVER_NOTICE);
    expect(again).toContain(CANCELLED_NOTICE);
    expect(again).toContain(successor!.id);
    const queued = await requestOf(orchestrator, 'queued');
    expect(queued.indexOf(REPEATED)).toBeGreaterThan(queued.lastIndexOf('E2E65-LONG'));
    expect(queued.indexOf('E2E65-QUEUED')).toBeGreaterThan(queued.indexOf(REPEATED));
    const runs = await testRuns(orchestrator, task!);
    expect(runs.map((run) => [run.session_id, run.state, run.error_code])).toEqual([
      [lost!.id, 'failed', 'device_failed'],
      [successor!.id, 'succeeded', null],
    ]);
    await expect(page.getByTestId('task.device-failure.failover')).toHaveAttribute('data-from', lost!.id, {
      timeout: 60_000,
    });
    await expect(page.getByTestId('task.device-failure.interrupted')).toHaveCount(0);

    // A stopped the orphan session: no live session of the task on A, nothing of the lost session after its end at
    // the hub (no transition, no further operation, the cancelled run unchanged).
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ n: number }>(
              `SELECT count(*)::int AS n FROM hub.session WHERE task_id = '${uuid(task!)}'
                  AND device_id = '${uuid(deviceA)}' AND state NOT IN ('stopped', 'failed')`,
            )
          )[0]!.n,
        { timeout: 60_000 },
      )
      .toBe(0);
    // The run A executed: the restarted runner ends it `error/restart` and keeps its log (ADR-087 section 5).
    await expect
      .poll(
        async () =>
          (
            await runnerStore<{ state: string; reason: string | null; logged: boolean }>(
              orchestrator,
              `SELECT state, result->>'reason' AS reason, (result->'log') IS NOT NULL
                      AND jsonb_typeof(result->'log') = 'object' AS logged
                 FROM runner.test_run WHERE operation_id = '${uuid(runA!.id)}'`,
              A,
            )
          )[0],
        { timeout: 180_000, intervals: [2000] },
      )
      .toEqual({ state: 'error', reason: 'restart', logged: true });
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ n: number }>(
              `SELECT count(*)::int AS n FROM hub.context_object WHERE session_id = '${uuid(lost!.id)}'
                  AND kind = 'log' AND state = 'complete'`,
            )
          )[0]!.n,
        { timeout: 180_000, intervals: [2000] },
      )
      .toBe(1);
    const [late] = await orchestrator.hubQuery<{ transitions: number; operations: number; events: number }>(
      `SELECT (SELECT count(*)::int FROM hub.session_transition t WHERE t.session_id = s.id
                 AND t.received_at > s.ended_at) AS transitions,
              (SELECT count(*)::int FROM hub.operation o WHERE o.session_id = s.id
                 AND o.created_at > s.ended_at) AS operations,
              (SELECT count(*)::int FROM hub.event e WHERE e.session_id = s.id
                 AND e.received_at > s.ended_at) AS events
         FROM hub.session s WHERE s.id = '${uuid(lost!.id)}'`,
    );
    console.log(`KOR58 lost session after its end: ${JSON.stringify(late)}`);
    expect({ transitions: late!.transitions, operations: late!.operations }).toEqual({ transitions: 0, operations: 0 });
    const [lostRow] = await sessionsOf(orchestrator, task!);
    expect(lostRow).toMatchObject({ state: 'stopped', state_reason: 'device_failed' });
    const [runRow] = await testRuns(orchestrator, task!);
    expect(runRow).toMatchObject({ state: 'failed', error_code: 'device_failed' });
    const [runTransitions] = await orchestrator.hubQuery<{ n: number }>(
      `SELECT count(*)::int AS n FROM hub.operation_transition WHERE operation_id = '${uuid(runA!.id)}'
          AND at > (SELECT ended_at FROM hub.session WHERE id = '${uuid(lost!.id)}')`,
    );
    expect(runTransitions!.n).toBe(0);

    await expectNoPlaintext(orchestrator, [
      'kz65-prompt-w3k8',
      'kz65-prompt-l5t2',
      'kz65-prompt-q7v4',
      FILE_CANARY,
      'kz65-answer-w2p9e7',
      'kz65-answer-q3s5k9',
      'kz65-log-a1m5',
      'kz65-wg-f4d7',
    ]);
  } finally {
    for (const name of spawned) await removeRunner(orchestrator, name).catch(() => undefined);
    await orchestrator.call('POST', '/runners/workvm', { enable: false }).catch(() => undefined);
    await orchestrator.setFailover(null, null).catch(() => undefined);
  }
  await closingChecks(orchestrator, true);
});

test('@E2E-67 @R-14.6 @needs-vm an organization whose only own device failed waits with the reason and continues there once the device is back, after a restart in the same session, after a reboot in a successor', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(2_400_000);
  let runner: string | null = null;
  try {
    await orchestrator.setFailover(ONLINE_SECONDS, FAILOVER_SECONDS);
    await orchestrator.loadScenario('model', ONE);
    runner = (await spawnRunner(orchestrator, { device: 'org', pool_size: 1 })).name;
    await authenticator('platform');
    await anonymousBudget(COST.register);
    const onboarded = await onboard(page, orchestrator, 'Ada kz65-name-s5q1');
    const org = onboarded.org;
    const user = await PageUser.open(page, onboarded.recoveryCode);
    const wg = await user.createWorkgroup(org, SOLO_WG_NAME);

    // The organization's own device, the only one of the workgroup; the owner's API key for it.
    const code = await addDeviceInvitation(page, org, [wg]);
    const device = await enrollRunner(orchestrator, runner, code);
    await expect(page.getByTestId('devices.add.status')).toHaveAttribute('data-enrolled', 'true', { timeout: 60_000 });
    await page.getByTestId('devices.add.done').click();
    await expectDevice(orchestrator, device, { state: 'active', online: true });
    expect((await user.log(org)).state.assigned(device, wg)).toBe(true);
    await storeApiKey(page, testApiKey());
    await poolOf(orchestrator, runner, device);

    const [task] = await user.createTasks(org, wg, [SOLO_WORK]);
    await navigate(page, `/o/${org}/t/${task!}`);
    await expectAnswerInChat(page, SOLO_ANSWER);
    const [lost] = await sessionsOf(orchestrator, task!);
    expect(lost!.device_id).toBe(device);
    await expect.poll(() => claudeId(orchestrator, lost!.id, runner!), { timeout: 60_000 }).toMatch(/^[0-9a-f-]{36}$/);
    const claude = (await claudeId(orchestrator, lost!.id, runner))!;
    await expect.poll(() => sessionState(orchestrator, lost!.id), { timeout: 120_000 }).toBe('idle');
    await checkpointed(orchestrator, task!, lost!.id, runner);

    // A working turn (the model double holds its answer), then the device fails hard.
    await followUp(page, SOLO_LONG);
    await expect.poll(() => consumed(orchestrator), { timeout: 120_000, intervals: [1000] }).toContain('solo-long');
    expect(await sessionState(orchestrator, lost!.id)).toBe('working');
    const killedAt = await hubNow(orchestrator);
    expect(await orchestrator.killRunner(runner)).toMatchObject({ exit: -9, state: 'stopped' });
    await expectDevice(orchestrator, device, { online: false }, 60_000);

    // After the deadline the task waits, interrupted because the device failed, and says so.
    await expect
      .poll(async () => (await sessionsOf(orchestrator, task!))[0]!.interrupted, {
        timeout: (FAILOVER_SECONDS + 4 * SWEEP_SECONDS) * 1000,
        intervals: [1000],
      })
      .toBe(true);
    await expect
      .poll(() => taskOf(user, task!), { timeout: 60_000 })
      .toEqual({
        state: 'scheduling',
        state_reason: 'device_failed',
      });
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ category: string; reason: string }>(
              `SELECT category, reason FROM hub.task_attention WHERE task_id = '${uuid(task!)}'`,
            )
          )[0],
        { timeout: 60_000 },
      )
      .toEqual({ category: 'blocked', reason: 'device_failed' });
    await expect(page.getByTestId('task.device-failure.interrupted')).toBeVisible({ timeout: 60_000 });
    // No other device of the organization: nothing moves (an org device's session never goes to a DFX device).
    await page.waitForTimeout(2 * SWEEP_SECONDS * 1000);
    const waiting = await sessionsOf(orchestrator, task!);
    expect(waiting.map((row) => [row.id, row.state, row.interrupted])).toEqual([[lost!.id, 'working', true]]);

    // The device is back with its VMs (the kill left them): the same session goes on there, the mark is cleared and
    // the held answer of the interrupted turn arrives.
    console.log(`KOR58 single_device_down_ms=${Math.round(await sinceMs(orchestrator, killedAt))} (kill to restart)`);
    await orchestrator.startRunner(runner);
    await expectDevice(orchestrator, device, { online: true });
    await expect
      .poll(async () => (await sessionsOf(orchestrator, task!)).map((row) => [row.id, row.interrupted]), {
        timeout: 4 * SWEEP_SECONDS * 1000,
        intervals: [1000],
      })
      .toEqual([[lost!.id, false]]);
    expect((await auditRows(orchestrator, task!, 'session.interruption_ended')).map((row) => row.session_id)).toEqual([
      lost!.id,
    ]);
    // the model double holds that answer 240 s from the request (scenario step `solo-long`)
    await expect(page.getByTestId('chat.output').filter({ hasText: SOLO_LONG_ANSWER })).toBeVisible({
      timeout: 300_000,
    });
    await expect(page.getByTestId('task.device-failure.interrupted')).toHaveCount(0, { timeout: 60_000 });
    expect((await taskOf(user, task!)).state_reason).not.toBe('device_failed');
    await expect.poll(() => sessionState(orchestrator, lost!.id), { timeout: 120_000 }).toBe('idle');
    await checkpointed(orchestrator, task!, lost!.id, runner);

    // A reboot: the device fails again during a turn and its VMs are gone when the runner starts. It reports the
    // session failed (`device_shutdown` or `guest_unreachable`), the hub starts the successor on the same device, which
    // continues the conversation with the interrupted instruction.
    const { vm } = await sessionVm(orchestrator, lost!.id);
    await followUp(page, SOLO_THIRD);
    await expect.poll(() => consumed(orchestrator), { timeout: 120_000, intervals: [1000] }).toContain('solo-third');
    const rebootedAt = await hubNow(orchestrator);
    expect(await orchestrator.killRunner(runner)).toMatchObject({ exit: -9, state: 'stopped' });
    await expectDevice(orchestrator, device, { online: false }, 60_000);
    await expect
      .poll(async () => (await sessionsOf(orchestrator, task!))[0]!.interrupted, {
        timeout: (FAILOVER_SECONDS + 4 * SWEEP_SECONDS) * 1000,
        intervals: [1000],
      })
      .toBe(true);
    // deletes the VM while the runner is down and starts the runner (routes/session_context.py)
    await orchestrator.call('POST', `/vms/${vm}/delete`);
    await expectDevice(orchestrator, device, { online: true });
    await expectAnswerInChat(page, SOLO_CONTINUED);
    const [failed, successor] = await sessionsOf(orchestrator, task!);
    expect(failed).toMatchObject({ id: lost!.id, state: 'failed' });
    expect(['device_shutdown', 'guest_unreachable']).toContain(failed!.state_reason);
    expect(successor).toMatchObject({ device_id: device, failover_of: lost!.id });
    const interruption = Math.round((await workingAfter(orchestrator, successor!.id, rebootedAt))!);
    console.log(`KOR58 single_device_interruption_ms=${interruption} (kill to working again after the reboot)`);
    await expect.poll(() => claudeId(orchestrator, successor!.id, runner!), { timeout: 60_000 }).toBe(claude);
    const again = await requestOf(orchestrator, 'solo-again');
    for (const earlier of [SOLO_WORK, 'I will keep the solo notes first.', SOLO_ANSWER, SOLO_LONG, SOLO_LONG_ANSWER]) {
      expect(again, `the resumed request lacks ${earlier}`).toContain(earlier);
    }
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    await expect(page.getByTestId('task.device-failure.interrupted')).toHaveCount(0, { timeout: 60_000 });
    expect((await taskOf(user, task!)).state_reason).not.toBe('device_failed');
  } finally {
    if (runner !== null) await removeRunner(orchestrator, runner).catch(() => undefined);
    await orchestrator.setFailover(null, null).catch(() => undefined);
  }
  await closingChecks(orchestrator, true);
});
