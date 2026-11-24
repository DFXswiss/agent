// E2E-38 (07-testing.md section 2.1; 03-api.md section 4.6.2; ADR-013): device assignment and scheduling. A workgroup
// has two devices – the run's platform device and a second runner instance with its own home on the runner device,
// a further certified platform device with more free capacity (no memory reserve) and a larger device ID, both
// assigned by the admin in the device screen's assignment dialog. The deterministic
// scheduler: (3) the device with the most free VM slots gets the first task (not the smallest ID), estimate 3000 ms
// with a ready pool VM; (1) the next task of the workgroup goes to the device with the workgroup's running VM (sticky),
// estimate 2500 ms; (2) once that VM has stopped (idle intervals of 60 s on that device), the device with the stopped VM
// of the workgroup, estimate 6500 ms, the VM restarted; only devices assigned to a workgroup are candidates – a
// workgroup with only the run's platform device runs there (3000 ms, pool VM) although the other device has more
// capacity; once the other device is assigned to that workgroup too, the workgroup's running VM still wins over the
// device with more free slots (sticky, 2500 ms). Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  anonymousBudget,
  closingChecks,
  COST,
  deviceItem,
  enrollRunner,
  expectDevice,
  navigate,
  removeRunner,
  spawnRunner,
} from '../fixtures/identity.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  home,
  invite,
  onboard,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-38-device-scheduling.json', import.meta.url), 'utf8'),
);
const WG_NAME = 'Platform only kz38-wg-w5e7r9';

interface Placed {
  readonly device_id: string;
  readonly estimate_ms: number;
  readonly vm_id: string | null;
}

interface Capacity {
  readonly id: string;
  readonly free: number;
  readonly memory: number;
  readonly pool: number;
}

test('@E2E-38 @R-0.3 @R-0.4 @R-3.3 @R-7.8 @R-14.6 @R-P.5 @needs-vm the scheduler chooses by capacity, keeps the workgroup on its VM and only uses assigned devices', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-38 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(1_200_000);
  await orchestrator.loadScenario('model', scenario);
  const shared = await platformDevice(orchestrator);
  await authenticator('platform');
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, 'Ada Scheduling');
  const org = onboarded.org;
  const user = await PageUser.open(page, onboarded.recoveryCode);
  const wg = (await user.log(org)).state.defaultWg;
  const sessionOf = async (task: string) =>
    (
      await orchestrator.hubQuery<Placed>(
        `SELECT device_id::text, estimate_ms, vm_id::text FROM hub.session WHERE task_id = '${uuid(task)}' AND depth = 0`,
      )
    )[0]!;
  const RUNNER = (
    await spawnRunner(orchestrator, {
      pool_size: 1,
      memory_reserve_mib: 0,
      session_idle_seconds: 60,
      vm_idle_seconds: 60,
    })
  ).name;
  try {
    // The second device: a further platform device (operator invitation, enrollment, certification).
    const own = await enrollRunner(orchestrator, RUNNER, (await invite(orchestrator, 'device')).code, true);
    expect(await operator(orchestrator, ['certify-pending', '--device', own])).toContain(`certified ${own}`);
    await expectDevice(orchestrator, own, { state: 'active', online: true });
    // Both join the default workgroup through the assignment dialog (candidates verified in the browser).
    await navigate(page, `/o/${org}/devices`);
    for (const device of [own, shared]) {
      await page.getByTestId('devices.assign.open').click();
      await page.getByTestId('devices.assign.wg').selectOption(wg);
      const candidate = page.locator(`[data-testid="devices.assign.candidate"][data-device="${device}"]`);
      await expect(candidate).toHaveAttribute('data-verified', 'true');
      await candidate.getByRole('radio').check();
      await page.getByTestId('devices.assign.confirm').click();
      await expect(page.getByTestId('devices.assign')).toHaveCount(0);
      await expect(deviceItem(page, device)).toBeVisible();
    }
    const log = await user.log(org);
    expect(log.state.assigned(own, wg) && log.state.assigned(shared, wg)).toBe(true);

    // Capacities differ: the second device has more free VM slots and the larger ID (enrolled later).
    await poolReady(orchestrator, 1);
    let capacities: Capacity[] = [];
    await expect
      .poll(
        async () => {
          capacities = await orchestrator.hubQuery<Capacity>(
            `SELECT id::text, (capacity->>'vm_slots_free')::int AS free,
                    (capacity->>'vm_memory_budget_mib')::int - (capacity->>'vm_memory_used_mib')::int AS memory,
                    (capacity->>'pool_ready')::int AS pool
               FROM hub.device WHERE id IN ('${uuid(own)}', '${uuid(shared)}')`,
          );
          const of = (id: string) => capacities.find((row) => row.id === id);
          return (of(own)?.free ?? 0) > (of(shared)?.free ?? 0) && (of(own)?.pool ?? 0) > 0;
        },
        { timeout: 120_000 },
      )
      .toBe(true);
    expect(own > shared).toBe(true);

    // (3) free capacity decides, estimate with a ready pool VM.
    await home(page);
    const first = await sendPrompt(page, 'E2E38-FIRST kz38-prompt-a2s4d6: hello');
    await expectAnswerInChat(page, 'Most capacity kz38-answer-f1g3h5.');
    const one = await sessionOf(first);
    expect(one).toMatchObject({ device_id: own, estimate_ms: 3000 });
    await expect.poll(async () => (await sessionOf(first)).vm_id, { timeout: 60_000 }).not.toBeNull();
    const vm = (await sessionOf(first)).vm_id;

    // (1) the next task of the workgroup goes to the device with the workgroup's running VM, into that VM.
    await home(page);
    const second = await sendPrompt(page, 'E2E38-STICKY kz38-prompt-z8x1c3: hello');
    await expectAnswerInChat(page, 'Same VM kz38-answer-j7k9l2.');
    await expect.poll(async () => (await sessionOf(second)).vm_id, { timeout: 60_000 }).toBe(vm);
    expect(await sessionOf(second)).toMatchObject({ device_id: own, estimate_ms: 2500 });
    const local = await runnerStore<{ n: number }>(
      orchestrator,
      `SELECT count(*)::int AS n FROM runner.vm WHERE state <> 'deleted' AND wg_id = '${uuid(wg)}'`,
      RUNNER,
    );
    expect(local).toEqual([{ n: 1 }]);

    // (2) the workgroup's VM stops (both sessions suspended after 60 s idle, the VM 60 s later): the next task goes to
    // the device with the stopped VM, which restarts.
    await expect
      .poll(
        async () =>
          (await orchestrator.hubQuery<{ state: string }>(`SELECT state FROM hub.vm WHERE id = '${vm}'`))[0]?.state,
        { timeout: 480_000, intervals: [5_000] },
      )
      .toBe('stopped');
    await home(page);
    const fourth = await sendPrompt(page, 'E2E38-STOPPED kz38-prompt-k2l4m6: hello');
    await expectAnswerInChat(page, 'Restarted VM kz38-answer-m3n5b7.');
    await expect.poll(async () => (await sessionOf(fourth)).vm_id, { timeout: 60_000 }).toBe(vm);
    expect(await sessionOf(fourth)).toMatchObject({ device_id: own, estimate_ms: 6500 });

    // Only assigned devices are candidates: a workgroup with only the platform device assigned at its creation.
    await navigate(page, `/o/${org}/workgroups`);
    await page.getByTestId('workgroups.create.open').click();
    await page.getByTestId('workgroups.create.name').fill(WG_NAME);
    await page.getByTestId(`workgroups.create.device.${shared}`).check();
    await page.getByTestId('workgroups.create.confirm').click();
    const created = (await page.getByTestId('workgroups.item').filter({ hasText: WG_NAME }).getAttribute('data-wg'))!;
    const after = await user.log(org);
    expect([after.state.assigned(shared, created), after.state.assigned(own, created)]).toEqual([true, false]);
    await poolReady(orchestrator, 1);
    await home(page);
    await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
    const third = await sendPrompt(page, 'E2E38-ASSIGNED kz38-prompt-v5b7n9: hello');
    await expectAnswerInChat(page, 'Assigned device kz38-answer-p4q6r8.');
    const placed = await sessionOf(third);
    expect(placed.device_id).toBe(shared);
    // (3) on the only candidate: no VM of the new workgroup yet, a ready pool VM
    expect(placed.estimate_ms).toBe(3000);
    await expect.poll(async () => (await sessionOf(third)).vm_id, { timeout: 60_000 }).not.toBeNull();
    const platformVm = (await sessionOf(third)).vm_id;

    // (1) against capacity: the device with more free slots joins the workgroup; its running VM on the run's platform
    // device keeps the next task there.
    await navigate(page, `/o/${org}/devices`);
    await page.getByTestId('devices.assign.open').click();
    await page.getByTestId('devices.assign.wg').selectOption(created);
    const more = page.locator(`[data-testid="devices.assign.candidate"][data-device="${own}"]`);
    await expect(more).toHaveAttribute('data-verified', 'true');
    await more.getByRole('radio').check();
    await page.getByTestId('devices.assign.confirm').click();
    await expect(page.getByTestId('devices.assign')).toHaveCount(0);
    expect((await user.log(org)).state.assigned(own, created)).toBe(true);
    const now = await orchestrator.hubQuery<Capacity>(
      `SELECT id::text, (capacity->>'vm_slots_free')::int AS free, 0 AS memory, 0 AS pool FROM hub.device
        WHERE id IN ('${uuid(own)}', '${uuid(shared)}')`,
    );
    const free = (id: string) => now.find((row) => row.id === id)!.free;
    expect(free(own)).toBeGreaterThan(free(shared));
    await home(page);
    await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
    const fifth = await sendPrompt(page, 'E2E38-STICKY-PLATFORM kz38-prompt-o4p6q8: hello');
    await expectAnswerInChat(page, 'Running VM wins kz38-answer-t8y1u3.');
    await expect.poll(async () => (await sessionOf(fifth)).vm_id, { timeout: 60_000 }).toBe(platformVm);
    expect(await sessionOf(fifth)).toMatchObject({ device_id: shared, estimate_ms: 2500 });
    await expectNoPlaintext(orchestrator, [
      'kz38-prompt-a2s4d6',
      'kz38-prompt-z8x1c3',
      'kz38-prompt-k2l4m6',
      'kz38-prompt-v5b7n9',
      'kz38-prompt-o4p6q8',
      'kz38-answer-f1g3h5',
      'kz38-answer-j7k9l2',
      'kz38-answer-m3n5b7',
      'kz38-answer-p4q6r8',
      'kz38-answer-t8y1u3',
      'kz38-wg-w5e7r9',
    ]);
  } finally {
    await removeRunner(orchestrator, RUNNER).catch(() => undefined);
  }
  await closingChecks(orchestrator, true);
});

// KOR-14 (05-runner.md section 3.4, 03-api.md section 4.6.2, ADR-049): a device that has no free VM slot refuses a
// session with `capacity_exhausted` and the hub re-schedules. A further runner with exactly one VM slot is the only
// device of two workgroups: workgroup A's VM has stopped (idle), workgroup B's VM runs and holds the slot. A task of A
// goes to the device (the scheduler places a workgroup with a stopped VM there although no slot is free, 03 section
// 4.6.2 (2)); restarting the VM needs a slot, B's session is not idle long enough to stop its VM, so the runner fails
// the session with `capacity_exhausted` - a real refusal, no database change. The hub starts the next attempt at once;
// the refusing device stays full for workgroup A until it reports a free slot, so the attempt waits and the browser
// shows the real reason; once B's VM has stopped the attempt starts on the restarted VM of A, the prompt is delivered
// once and answered. Runs on T1 and T2.
const REFUSAL_SCENARIO: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-38-capacity-refusal.json', import.meta.url), 'utf8'),
);
const WG_A = 'Slot A kz38-wg-a6s8d1';
const WG_B = 'Slot B kz38-wg-b3n5m7';

interface Attempt {
  readonly state: string;
  readonly state_reason: string | null;
  readonly device_id: string | null;
  readonly first_reason: string | null;
}

test('@E2E-38 @R-0.3 @R-3.12 @R-7.8 @needs-vm a device without a free slot refuses a session and the hub re-schedules it once the slot is free', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', REFUSAL_SCENARIO);
  await authenticator('platform');
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, 'Ada Capacity');
  const org = onboarded.org;
  const RUNNER = (await spawnRunner(orchestrator, { pool_size: 0, session_idle_seconds: 60, vm_idle_seconds: 60 }))
    .name;
  try {
    const own = await enrollRunner(orchestrator, RUNNER, (await invite(orchestrator, 'device')).code, true);
    expect(await operator(orchestrator, ['certify-pending', '--device', own])).toContain(`certified ${own}`);
    // exactly one VM slot on every runner of the run (the runners restart with the same homes)
    await orchestrator.call('POST', '/runners/capacity', { slots: 1 });
    await expectDevice(orchestrator, own, { state: 'active', online: true });
    const slots = async () =>
      (
        await orchestrator.hubQuery<{ total: number; free: number }>(
          `SELECT (capacity->>'vm_slots_total')::int AS total, (capacity->>'vm_slots_free')::int AS free
             FROM hub.device WHERE id = '${uuid(own)}'`,
        )
      )[0];
    await expect.poll(slots, { timeout: 120_000, intervals: [2000] }).toEqual({ total: 1, free: 1 });

    // the device joins the org through the assignment dialog (default workgroup), then two workgroups with only it
    const user = await PageUser.open(page, onboarded.recoveryCode);
    await navigate(page, `/o/${org}/devices`);
    await page.getByTestId('devices.assign.open').click();
    await page.getByTestId('devices.assign.wg').selectOption((await user.log(org)).state.defaultWg);
    const candidate = page.locator(`[data-testid="devices.assign.candidate"][data-device="${own}"]`);
    await expect(candidate).toHaveAttribute('data-verified', 'true');
    await candidate.getByRole('radio').check();
    await page.getByTestId('devices.assign.confirm').click();
    await expect(page.getByTestId('devices.assign')).toHaveCount(0);
    await expect(deviceItem(page, own)).toBeVisible();
    const wgs: string[] = [];
    for (const name of [WG_A, WG_B]) {
      await navigate(page, `/o/${org}/workgroups`);
      await page.getByTestId('workgroups.create.open').click();
      await page.getByTestId('workgroups.create.name').fill(name);
      await page.getByTestId(`workgroups.create.device.${own}`).check();
      await page.getByTestId('workgroups.create.confirm').click();
      wgs.push((await page.getByTestId('workgroups.item').filter({ hasText: name }).getAttribute('data-wg'))!);
    }
    const [wgA] = wgs;
    const vmOf = async (wg: string) =>
      (
        await orchestrator.hubQuery<{ id: string; state: string }>(
          `SELECT id, state FROM hub.vm WHERE wg_id = '${uuid(wg)}' AND state <> 'deleted'`,
        )
      )[0] ?? null;
    const taskIn = async (wg: string, prompt: string) => {
      await home(page);
      await page.getByTestId('home.wg').selectOption({ label: wg });
      return sendPrompt(page, prompt);
    };

    // workgroup A runs once; its VM stops after the idle intervals and gives the slot back
    await taskIn(WG_A, 'E2E38-CAP-A1 kz38-prompt-c2d4f6: hello');
    await expectAnswerInChat(page, 'Workgroup A ready kz38-answer-c1a2p3.');
    await expect.poll(async () => (await vmOf(wgA!))?.state, { timeout: 480_000, intervals: [5_000] }).toBe('stopped');
    const vmA = (await vmOf(wgA!))!.id;
    await expect.poll(async () => (await slots())?.free, { timeout: 60_000, intervals: [2000] }).toBe(1);

    // workgroup B takes the only slot
    await taskIn(WG_B, 'E2E38-CAP-B kz38-prompt-g8h1j3: hello');
    await expectAnswerInChat(page, 'Workgroup B holds the slot kz38-answer-c4b5s6.');
    await expect.poll(async () => (await slots())?.free, { timeout: 60_000, intervals: [1000] }).toBe(0);

    // the next task of A: placed on the device (stopped VM of A), refused there, re-scheduled and waiting
    const refused = await taskIn(WG_A, 'E2E38-CAP-A2 kz38-prompt-k5l7z9: hello');
    const attempts = () =>
      orchestrator.hubQuery<Attempt>(
        `SELECT s.state, s.state_reason, s.device_id::text,
                (SELECT t.reason FROM hub.session_transition t WHERE t.session_id = s.id AND t.seq = 1) AS first_reason
           FROM hub.session s WHERE s.task_id = '${uuid(refused)}' AND s.depth = 0 ORDER BY s.created_at, s.id`,
      );
    await expect
      .poll(async () => (await attempts()).map((row) => [row.state, row.state_reason, row.first_reason]), {
        timeout: 120_000,
        intervals: [1000],
      })
      .toEqual([
        ['failed', 'capacity_exhausted', null],
        ['requested', null, 'capacity_exhausted'],
      ]);
    expect((await attempts())[0]!.device_id).toBe(own);
    const [task] = await orchestrator.hubQuery<{ state: string; state_reason: string | null }>(
      `SELECT state, state_reason FROM hub.task WHERE id = '${uuid(refused)}'`,
    );
    expect(task).toEqual({ state: 'scheduling', state_reason: 'capacity_exhausted' });
    // the browser shows the real reason while the attempt waits
    await expect(page.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'capacity_exhausted', {
      timeout: 30_000,
    });

    // B's session is suspended and its VM stops: the device reports the free slot, the attempt starts on A's VM
    await expect(page.getByTestId('chat.output').filter({ hasText: 'kz38-answer-c7r8f9' })).toBeVisible({
      timeout: 480_000,
    });
    const done = await attempts();
    expect(done.map((row) => [row.state === 'failed' ? 'failed' : 'ran', row.device_id])).toEqual([
      ['failed', own],
      ['ran', own],
    ]);
    expect((await vmOf(wgA!))?.id).toBe(vmA);
    // the first prompt was accepted exactly once, and answered once
    const [working] = await orchestrator.hubQuery<{ n: number }>(
      `SELECT count(*)::int AS n FROM hub.session_transition t JOIN hub.session s ON s.id = t.session_id
        WHERE s.task_id = '${uuid(refused)}' AND t.state = 'working'`,
    );
    expect(working).toEqual({ n: 1 });
    await expect(page.getByTestId('chat.output').filter({ hasText: 'kz38-answer-c7r8f9' })).toHaveCount(1);
    await expectNoPlaintext(orchestrator, [
      'kz38-prompt-c2d4f6',
      'kz38-prompt-g8h1j3',
      'kz38-prompt-k5l7z9',
      'kz38-answer-c1a2p3',
      'kz38-answer-c4b5s6',
      'kz38-answer-c7r8f9',
      'kz38-wg-a6s8d1',
      'kz38-wg-b3n5m7',
    ]);
  } finally {
    await orchestrator.call('POST', '/runners/capacity', { slots: null }).catch(() => undefined);
    await removeRunner(orchestrator, RUNNER).catch(() => undefined);
  }
  await closingChecks(orchestrator, true);
});
