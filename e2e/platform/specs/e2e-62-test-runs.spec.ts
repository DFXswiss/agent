// E2E-62 (KOR-43; docs/platform/spec/13-work-vm.md section 12, ADR-080): test runs as platform operations. The runner
// offers work VMs in self mode (TEST_MODE, no fixed number of VMs, a warm pool of two) from the run's work image cache. The
// session checks out a repository through the platform, runs one command in its work VM (an implicit start that
// announces itself), then runs the tests with the operation `test.run`: the first run fails and prints more than
// 1 MiB, the session fixes the code, the second run passes in a fresh VM, and the session opens a draft pull request.
// Both run VMs are gone right after their runs; the complete log is kept encrypted, opens in the browser and is the
// same bytes the run printed; closing the task deletes the work VM at once (`task_closed`, or `session_ended` when the stop of the
// session came first). The measured start times of the
// run VMs go to the iteration's artefacts (`e2e-62/test-runs.json`). T1 only.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import { allowRepositories, connectGitHub, githubRepo, repository, seedGitHub, worldOf } from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  home,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-62-test-runs.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz60-octo-d3f7g1h5';
const REPO = 'octo-org/calc';
const PROMPT = 'E2E60-TESTS kz60-prompt-j6k2l8z4: run the tests of octo-org/calc, fix the failure and open a draft PR';
const LOG_LINE = 'kz60-log-v3b7n1m5 line ';
const LINES = 50_000;
const CANARIES = [
  LOGIN,
  'kz60-prompt-j6k2l8z4',
  'kz60-log-v3b7n1m5',
  'kz60-file-x7c3v9b1',
  'kz60-answer-q2w6e8r4',
  'kz60-name-t5y9u3i7',
];
const FILES = {
  'calc.py': 'def add(a, b):\n    return a - b\n',
  'test_calc.py':
    'import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n' +
    '        self.assertEqual(add(2, 3), 5)\n\n\nunittest.main()\n',
};

// Closing the task deletes the VM at once: by the closed task, or by the end of its session when that came first.
const CLOSED = new Set(['task_closed', 'session_ended']);

interface RunResult {
  readonly run_id: string;
  readonly state: string;
  readonly exit_code: number | null;
  readonly ready_ms: number;
  readonly duration_ms: number;
  readonly log: { object_id: string; bytes: number; stored_bytes: number; truncated: boolean } | null;
  readonly tail: string;
}

interface VmRow {
  readonly scope: string;
  readonly state: string;
  readonly delete_reason: string | null;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-62 @needs-vm @needs-workvm tests run red then green in fresh VMs, the log is kept and opens, the VMs go', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-62 is a T1 test (work VMs in self mode on the runner device)');
  test.setTimeout(1_500_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(
    orchestrator,
    worldOf(LOGIN, 'octo-org', [
      repository(REPO, { branches: [{ name: 'main', commits: [{ message: 'Add calc.', files: FILES }] }] }),
    ]),
  );
  await platformDevice(orchestrator);
  await orchestrator.call('POST', '/runners/workvm', { enable: true, pool_size: 2 });
  await poolReady(orchestrator);
  await expect
    .poll(
      async () =>
        (
          await runnerStore<{ n: number }>(
            orchestrator,
            "SELECT count(*)::int AS n FROM runner.workvm_pool WHERE state = 'ready'",
          )
        )[0]!.n,
      { timeout: 900_000, intervals: [5_000] },
    )
    .toBe(2);

  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz60-name-t5y9u3i7');
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [REPO]);

  // 1. Work VM once, then test.run red → fix → test.run green → draft pull request, all through the platform.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'draft pull request #1 is open kz60-answer-q2w6e8r4.');
  const repo = await githubRepo(orchestrator, REPO);
  expect(repo.pulls.map((pull) => [pull.number, pull.draft, pull.head])).toEqual([[1, true, 'e2e60/fix']]);

  // The two runs as the user decrypts them: red with a log over 1 MiB, green; each in its own VM.
  const results = (await taskBodies(page, orchestrator, onboarded, task, 'operation.result'))
    .map((body) => body.result as RunResult | null)
    .filter((result): result is RunResult => result !== null && typeof result.run_id === 'string');
  expect(results.map((result) => [result.state, result.exit_code])).toEqual([
    ['failed', 1],
    ['passed', 0],
  ]);
  const red = results[0]!;
  const expectedBytes = Array.from({ length: LINES }, (_, index) => `${LOG_LINE}${index + 1}\n`).join('').length;
  expect(red.log).not.toBeNull();
  expect(red.log!.bytes).toBeGreaterThan(1024 * 1024);
  expect(red.log!.bytes).toBeGreaterThan(expectedBytes);
  expect(red.log!.truncated).toBe(false);
  expect(red.tail).toContain('FAILED (failures=1)');
  const events = (await taskBodies(page, orchestrator, onboarded, task, 'test.run')).filter(
    (body) => body.state !== 'running',
  );
  expect(events.map((body) => [body.run_id, body.state])).toEqual(
    results.map((result) => [result.run_id, result.state]),
  );

  // Both run VMs went right after their runs; the work VM still runs.
  await expect
    .poll(
      async () =>
        runnerStore<VmRow>(
          orchestrator,
          `SELECT scope, state, delete_reason FROM runner.workvm WHERE task_id = '${uuid(task)}' ORDER BY created_at`,
        ),
      { timeout: 120_000, intervals: [2_000] },
    )
    .toEqual([
      { scope: 'task', state: 'running', delete_reason: null },
      { scope: 'run', state: 'deleted', delete_reason: 'run_ended' },
      { scope: 'run', state: 'deleted', delete_reason: 'run_ended' },
    ]);
  const runs = await runnerStore<{ state: string }>(
    orchestrator,
    `SELECT state FROM runner.test_run WHERE task_id = '${uuid(task)}' ORDER BY started_at`,
  );
  expect(runs.map((row) => row.state)).toEqual(['failed', 'passed']);

  // The implicit start of the work VM announced itself (the panel's countdown), then it was created.
  expect(
    (await taskBodies(page, orchestrator, onboarded, task, 'workvm.state')).map((body) => [body.state, body.implicit]),
  ).toEqual([
    ['starting', true],
    ['created', true],
  ]);

  // The chat shows both runs; the log of the red run opens in the browser with its last line, and downloads whole.
  await expect(page.getByTestId('chat.test.run')).toHaveCount(2, { timeout: 30_000 });
  await page.getByTestId('chat.test.run').first().getByTestId('log.open').click();
  const viewer = page.getByTestId('log.viewer');
  await expect(viewer).toBeVisible();
  await viewer.getByTestId('log.last').click();
  await expect(viewer).toContainText(`${LOG_LINE}${LINES}`, { timeout: 60_000 });
  const download = page.waitForEvent('download');
  await viewer.getByTestId('log.download').click();
  const file = await (await download).path();
  const text = readFileSync(file, 'utf8');
  expect(text.startsWith(`${LOG_LINE}1\n${LOG_LINE}2\n`)).toBe(true);
  expect(text).toContain(`${LOG_LINE}${LINES}\n`);
  expect(Buffer.byteLength(text)).toBe(red.log!.bytes);
  await viewer.getByTestId('log.close').click();

  // 2. Closing the task deletes its work VM at once.
  await page.getByTestId('task.close').click();
  await page.getByTestId('task.close.confirm').click();
  await expect
    .poll(
      async () =>
        (
          await runnerStore<VmRow>(
            orchestrator,
            `SELECT scope, state, delete_reason FROM runner.workvm WHERE scope = 'task' AND task_id = '${uuid(task)}'`,
          )
        ).map((row) => ({ ...row, delete_reason: CLOSED.has(row.delete_reason ?? '') ? 'closed' : row.delete_reason })),
      { timeout: 180_000, intervals: [2_000] },
    )
    .toEqual([{ scope: 'task', state: 'deleted', delete_reason: 'closed' }]);

  const dir = join(e2e.iterationDir, 'e2e-62');
  mkdirSync(dir, { recursive: true });
  const measured = {
    run_ready_ms: results.map((result) => result.ready_ms),
    run_duration_ms: results.map((result) => result.duration_ms),
    log_bytes: red.log!.bytes,
  };
  writeFileSync(join(dir, 'test-runs.json'), JSON.stringify(measured, null, 1));
  await expectNoPlaintext(orchestrator, CANARIES);
});
