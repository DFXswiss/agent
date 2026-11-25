// E2E-69 golden switch (KOR-70; ADR-093; concept ideas 39, 40): a workgroup VM on an older golden moves to the
// device's current golden by itself once every session on it is paused with a complete checkpoint, and the paused
// sessions move with it. Two tasks of one workgroup each write a file on the same VM; the VM is marked as cloned from
// an older golden (`POST /runners/runner-1/vm-outdated`) while both sessions are still idle: the VM waits and the
// workgroup screen says which session holds it since when. Once both are paused the runner makes a VM of the current
// golden ready, then deletes the old one (`replaced_by` at the hub). A follow-up of each task resumes the SAME session
// and conversation on the new VM: the earlier turn is in the model's request, the file is read in the new sandbox,
// the hub row says `golden_switch` and the task shows the one line "Continued on the new version …". The hub side
// holds none of the texts (plaintext scan). Runs on T1. Hooks: orchestrator/routes/golden_switch.py.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, osRead, runnerStore, sessionState, setIdle } from '../fixtures/guest.ts';
import { navigate } from '../fixtures/identity.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const SCENARIO: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-69-golden-switch.json', import.meta.url), 'utf8'),
);
const NAME = 'Ada kz69-name-v7q2';
// Long enough that the runner's first switch pass (every 60 s) still sees the idle sessions (the hold), short enough
// that the run does not wait long for the pause.
const IDLE_SECONDS = 240;
const SWITCH_TIMEOUT = 360_000;
const OUTDATED = 'e2e-outdated';
const GOLDEN_LINE = 'Continued on the new version of the workgroup VM – files carried over.';

interface Turn {
  readonly prompt: string;
  readonly again: string;
  readonly file: string;
  readonly canary: string;
  readonly answer: string;
  readonly continued: string;
}

const ALPHA: Turn = {
  prompt: 'KZ69-ALPHA-WORK kz69-prompt-alpha-d3s8: start alpha.md',
  again: 'KZ69-ALPHA-AGAIN kz69-again-alpha-j6f1: what does alpha.md hold?',
  file: 'alpha.md',
  canary: 'kz69-file-alpha-r4t8',
  answer: 'Alpha is written kz69-answer-alpha-p2m6.',
  continued: 'Continued: alpha still holds kz69-file-alpha-r4t8.',
};
const BETA: Turn = {
  prompt: 'KZ69-BETA-WORK kz69-prompt-beta-x5k3: start beta.md',
  again: 'KZ69-BETA-AGAIN kz69-again-beta-q8w2: what does beta.md hold?',
  file: 'beta.md',
  canary: 'kz69-file-beta-w3n9',
  answer: 'Beta is written kz69-answer-beta-h5c1.',
  continued: 'Continued: beta still holds kz69-file-beta-w3n9.',
};
const CANARIES = [
  'kz69-name-v7q2',
  ...[ALPHA, BETA].flatMap((turn) => [turn.canary, turn.answer.split(' ').at(-1)!.replace(/\.$/, '')]),
  'kz69-prompt-alpha-d3s8',
  'kz69-prompt-beta-x5k3',
];

interface Started {
  readonly turn: Turn;
  readonly task: string;
  readonly session: string;
}

interface Upgrade {
  readonly state: string;
  readonly holds: { readonly reason: string; readonly session_id?: string; readonly since: string }[];
  readonly sessions: string[];
}

/** The runner's VM of the session. */
async function runnerVm(orchestrator: Orchestrator, session: string): Promise<string | null> {
  const rows = await runnerStore<{ vm_id: string | null }>(
    orchestrator,
    `SELECT vm_id FROM runner.session WHERE id = '${uuid(session)}'`,
  );
  return rows[0]?.vm_id ?? null;
}

/** The runner's row of the VM (null: unknown). */
async function vmRow(
  orchestrator: Orchestrator,
  vm: string,
): Promise<{ state: string; golden_version: string } | null> {
  const rows = await runnerStore<{ state: string; golden_version: string }>(
    orchestrator,
    `SELECT state, golden_version FROM runner.vm WHERE id = '${vm.replace(/[^a-z0-9-]/g, '')}'`,
  );
  return rows[0] ?? null;
}

/** The VM's switch status and successor as the hub stored them. */
async function hubVm(
  orchestrator: Orchestrator,
  vm: string,
): Promise<{ upgrade: Upgrade | null; replaced_by: string | null } | null> {
  const rows = await orchestrator.hubQuery<{ upgrade: Upgrade | null; replaced_by: string | null }>(
    `SELECT upgrade, replaced_by FROM hub.vm WHERE id = '${vm.replace(/[^a-z0-9-]/g, '')}'`,
  );
  return rows[0] ?? null;
}

/** Whether the runner holds a complete checkpoint of the session (nothing due, the whole workspace). */
async function complete(orchestrator: Orchestrator, session: string): Promise<boolean> {
  const rows = await runnerStore<{ ok: boolean }>(
    orchestrator,
    `SELECT due_at IS NULL AND checkpoint_state = 'full' AS ok FROM runner.context_session
      WHERE session_id = '${uuid(session)}'`,
  );
  return rows[0]?.ok ?? false;
}

async function start(page: import('@playwright/test').Page, orchestrator: Orchestrator, turn: Turn): Promise<Started> {
  const task = await sendPrompt(page, turn.prompt);
  await expectAnswerInChat(page, turn.answer);
  const [main] = await mainSessions(orchestrator, task);
  return { turn, task, session: main!.id };
}

test('@E2E-69 @R-6.3 @needs-vm a workgroup VM on an older golden moves to the current one by itself and two paused sessions continue there with their files', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_500_000);
  await setIdle(orchestrator, IDLE_SECONDS);
  try {
    await orchestrator.loadScenario('model', SCENARIO);
    await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const { org } = await onboard(page, orchestrator, NAME);
    const alpha = await start(page, orchestrator, ALPHA);
    await home(page);
    const beta = await start(page, orchestrator, BETA);
    const vm = (await runnerVm(orchestrator, alpha.session))!;
    expect(vm).toMatch(/^vm-[0-9a-f]{8}$/);
    expect(await runnerVm(orchestrator, beta.session), 'both tasks share the workgroup VM').toBe(vm);

    // The VM is on an older golden while its sessions are still idle: it waits and says who holds it, since when.
    await orchestrator.call('POST', '/runners/runner-1/vm-outdated', { vm_id: vm });
    await expect
      .poll(
        async () =>
          ((await hubVm(orchestrator, vm))?.upgrade?.holds ?? [])
            .filter((hold) => hold.reason === 'session_active')
            .map((hold) => hold.session_id)
            .sort(),
        { timeout: 180_000, intervals: [2000] },
      )
      .toEqual([alpha.session, beta.session].sort());
    const waiting = (await hubVm(orchestrator, vm))!.upgrade!;
    expect(waiting.state).toBe('waiting');
    expect(waiting.holds.map((hold) => hold.session_id).sort()).toEqual([alpha.session, beta.session].sort());
    await navigate(page, `/o/${org}/workgroups`);
    const row = page.locator(`[data-testid="workgroups.vm-switch.item"][data-vm="${vm}"]`);
    await expect(row).toHaveAttribute('data-status', 'held', { timeout: 30_000 });
    await expect(row.getByTestId('workgroups.vm-switch.hold')).toHaveCount(2);

    // Both sessions pause, their checkpoints are complete, and the VM moves without anyone acting: the new VM of the
    // current golden runs before the old one is deleted (the hub names the successor).
    for (const { session } of [alpha, beta]) {
      await expect
        .poll(() => sessionState(orchestrator, session), { timeout: (IDLE_SECONDS + 120) * 1000, intervals: [2000] })
        .toBe('suspended');
      await expect.poll(() => complete(orchestrator, session), { timeout: 240_000, intervals: [2000] }).toBe(true);
    }
    await expect
      .poll(async () => (await vmRow(orchestrator, vm))?.state, { timeout: SWITCH_TIMEOUT, intervals: [3000] })
      .toBe('deleted');
    const [switched] = await runnerStore<{ new_vm_id: string; golden_version: string }>(
      orchestrator,
      `SELECT new_vm_id, golden_version FROM runner.golden_switch WHERE old_vm_id = '${vm}'`,
    );
    expect(switched, 'the switch is recorded').toBeDefined();
    const fresh = (await vmRow(orchestrator, switched!.new_vm_id))!;
    expect(fresh.golden_version).toBe(switched!.golden_version);
    expect(fresh.golden_version).not.toBe(OUTDATED);
    await expect
      .poll(async () => (await hubVm(orchestrator, vm))?.replaced_by, { timeout: 60_000 })
      .toBe(switched!.new_vm_id);

    // A follow-up of each task resumes the same session and conversation on the new VM, with its file.
    for (const { turn, task, session } of [alpha, beta]) {
      await navigate(page, `/o/${org}/t/${task}`);
      await page.getByTestId('chat.composer.prompt').fill(turn.again);
      await page.getByTestId('chat.composer.send').click();
      await expectAnswerInChat(page, turn.continued);
      expect((await mainSessions(orchestrator, task)).map((found) => found.id)).toEqual([session]);
      expect(await runnerVm(orchestrator, session)).toBe(switched!.new_vm_id);
      const read = await osRead(orchestrator, session, `/srv/sessions/${session}/work/${turn.file}`);
      expect(read.in_sandbox).toMatchObject({ ok: true });
      expect(read.in_sandbox.data).toContain(turn.canary);
      const [stored] = await orchestrator.hubQuery<{ context_restore: string | null; context_reason: string | null }>(
        `SELECT context_restore, context_reason FROM hub.session WHERE id = '${uuid(session)}'`,
      );
      expect(stored).toEqual({ context_restore: 'restored', context_reason: 'golden_switch' });
      const audits = (await auditRows(orchestrator, task, 'session.context_restored')).filter(
        (audit) => audit.session_id === session,
      );
      expect(audits.at(-1)!.detail).toMatchObject({ workspace: 'full', stale: false, moved: 'golden' });
      expect(await auditRows(orchestrator, task, 'session.context_fallback')).toEqual([]);
      await expect(page.getByTestId('task.context.restored')).toHaveText(GOLDEN_LINE, { timeout: 30_000 });
    }
    // The model saw each earlier turn in the resumed conversation, not a summary.
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

    await expectNoPlaintext(orchestrator, CANARIES);
  } finally {
    await setIdle(orchestrator, null);
  }
});
