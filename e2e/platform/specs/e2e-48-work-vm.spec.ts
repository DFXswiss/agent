// E2E-41 (KOR-29; agent docs/platform/spec/11-work-vm.md, ADR-056): the work VM on request. The runner offers work
// VMs in self mode (TEST_MODE, two slots, one per task, a warm pool of one) from the run's work image cache. The
// session checks out a repository through the platform, requests a work VM (answered from the warm pool), runs the
// failing tests as root, fixes the code in its workspace, runs them green and opens a draft pull request through
// the GitHub operations. A follow-up prompt of the same task uses the same VM: it starts a program as a service,
// reads its page with the browser of the VM and shows the VM's status; a second VM for the task is refused with
// `limit_exceeded`. The chat shows every run; closing the task tears the VM down (`session_ended`). The measured
// times (assignment, cold start of a pool entry) go to the iteration's artefacts (`e2e-41/work-vm.json`). T1 only.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import { allowRepositories, connectGitHub, githubRepo, repository, seedGitHub, worldOf } from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-41-work-vm.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz41-octo-a6s2d9';
const REPO = 'octo-org/calc';
const PROMPT = 'E2E41-FIX kz41-prompt-w7e3r5t1: run the tests of octo-org/calc, fix the failure and open a draft PR';
const FOLLOW_UP = 'E2E41-SERVICE kz41-follow-y4u8i2o6: start a small web service and tell me what its page says';
const CANARIES = [
  LOGIN,
  'kz41-prompt-w7e3r5t1',
  'kz41-follow-y4u8i2o6',
  'kz41-file-m2n6b8v4',
  'kz41-page-r5t8y2u4',
  'kz41-answer-h3j7k1l5',
  'kz41-answer-p8o2i6u3',
  'kz41-name-q1w5e9r3',
];
const FILES = {
  'calc.py': 'def add(a, b):\n    return a - b\n',
  'test_calc.py':
    'import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n' +
    '        self.assertEqual(add(2, 3), 5)\n\n\nunittest.main()\n',
};

interface WorkvmRow {
  readonly state: string;
  readonly delete_reason: string | null;
  readonly size: string;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-41 @needs-vm @needs-workvm the session gets a work VM, runs and fixes the tests, serves and browses a page, and the VM goes with the task', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-41 is a T1 test (work VMs in self mode on the runner device)');
  test.setTimeout(1_200_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(
    orchestrator,
    worldOf(LOGIN, 'octo-org', [
      repository(REPO, { branches: [{ name: 'main', commits: [{ message: 'Add calc.', files: FILES }] }] }),
    ]),
  );
  await platformDevice(orchestrator);
  await orchestrator.call('POST', '/runners/workvm', { slots: 2, max_per_task: 1, pool_size: 1 });
  await poolReady(orchestrator);
  // the warm pool: one started work VM waits for the first request (its cold start is measured below)
  await expect
    .poll(
      async () =>
        (
          await runnerStore<{ n: number }>(
            orchestrator,
            "SELECT count(*)::int AS n FROM runner.workvm_pool WHERE state = 'ready'",
          )
        )[0]!.n,
      { timeout: 600_000, intervals: [5_000] },
    )
    .toBe(1);
  const [cold] = await runnerStore<{ s: number }>(
    orchestrator,
    'SELECT extract(epoch FROM ready_at - created_at)::float AS s FROM runner.workvm_pool',
  );

  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz41-name-q1w5e9r3');
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [REPO]);

  // 1. Tests red → fix → green → draft pull request, all through the platform.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'draft pull request #1 is open kz41-answer-h3j7k1l5.');
  const repo = await githubRepo(orchestrator, REPO);
  expect(repo.pulls.map((pull) => [pull.number, pull.draft, pull.head])).toEqual([[1, true, 'e2e41/fix']]);

  // 2. The follow-up uses the same VM: a service, the browser, the status, and the per-task limit.
  await page.getByTestId('chat.composer.prompt').fill(FOLLOW_UP);
  await page.getByTestId('chat.composer.send').click();
  await expectAnswerInChat(page, 'A second work VM is above the limit of this task kz41-answer-p8o2i6u3.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);

  // The request was answered from the pool; the operations as the user decrypts them.
  const results = await taskBodies(page, orchestrator, onboarded, task, 'operation.result');
  const requested = results.filter(
    (body) => (body.result as { workvm_id?: string } | null)?.workvm_id !== undefined,
  ) as { result: { created: boolean; ready_ms: number; size: string } }[];
  expect(requested.map((body) => [body.result.created, body.result.size])).toEqual([[true, 'standard']]);
  const readyMs = requested[0]!.result.ready_ms;
  expect(readyMs).toBeLessThan(10_000);
  const refused = results.filter((body) => body.status === 'failed') as { error: { code: string } }[];
  expect(refused.map((body) => body.error.code)).toEqual(['limit_exceeded']);

  // Every run is recorded (command, final state, exit code) and shown in the chat; one VM was created.
  const runs = (await taskBodies(page, orchestrator, onboarded, task, 'workvm.run')).filter(
    (body) => body.state !== 'running',
  );
  expect(runs.map((body) => [body.kind, body.state, body.exit_code])).toEqual([
    ['exec', 'exited', 1],
    ['exec', 'exited', 0],
    ['exec', 'exited', 0],
    ['browse', 'exited', 0],
  ]);
  expect(String(runs[2]!.output)).toContain('Hello kz41-page-r5t8y2u4');
  await expect(page.getByTestId('chat.workvm.run')).toHaveCount(4, { timeout: 30_000 });
  const created = (await taskBodies(page, orchestrator, onboarded, task, 'workvm.state')).filter(
    (body) => body.state === 'created',
  );
  expect(created.length).toBe(1);
  expect(created[0]!.workvm_id).toBe(runs[0]!.workvm_id);
  expect(new Set(runs.map((body) => body.workvm_id)).size).toBe(1);

  // 3. Closing the task ends the session; the VM goes with it.
  await page.getByTestId('task.close').click();
  await page.getByTestId('task.close.confirm').click();
  await expect
    .poll(async () => runnerStore<WorkvmRow>(orchestrator, 'SELECT state, delete_reason, size FROM runner.workvm'), {
      timeout: 180_000,
      intervals: [2_000],
    })
    .toEqual([{ state: 'deleted', delete_reason: 'session_ended', size: 'standard' }]);
  await expect
    .poll(
      async () =>
        (await taskBodies(page, orchestrator, onboarded, task, 'workvm.state')).map((body) => [
          body.state,
          body.reason,
        ]),
      { timeout: 60_000 },
    )
    .toEqual([
      ['created', null],
      ['deleted', 'session_ended'],
    ]);

  const dir = join(e2e.iterationDir, 'e2e-41');
  mkdirSync(dir, { recursive: true });
  const measured = { session: session!.id, assignment_ready_ms: readyMs, pool_cold_start_s: cold!.s };
  writeFileSync(join(dir, 'work-vm.json'), JSON.stringify(measured, null, 1));
  await expectNoPlaintext(orchestrator, CANARIES);
});
