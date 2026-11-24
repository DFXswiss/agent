// E2E-40 (07-testing.md section 2.1; 03-api.md section 7.2 `session.file`; 05-runner.md sections 6.4 and 12.1): a
// checkout is not a file change of the session. The session checks out a repository of 3000 files from the GitHub
// double (`github.checkout`, `files/import` into the workspace), then writes one new file inside the checked-out
// directory with the Write tool. The guest's workspace scanner takes the imported tree as its baseline: neither the
// hub nor the decrypted timeline holds a `session.file` record for any of the 3000 files, and the chat shows exactly
// one file change, the session's own write, with its patch expandable. Runs on T1.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  allowRepositories,
  connectGitHub,
  expectGitHubOk,
  hubOperations,
  repository,
  seedGitHub,
  worldOf,
} from '../fixtures/github.ts';
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
  readFileSync(new URL('../scenarios/e2e-40-checkout-quiet.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz40-octo-q7m2x9';
const REPO = 'octo-org/quiet';
const DIR = 'repos/octo-org/quiet';
const PROMPT = 'E2E40-QUIET kz40-prompt-d2k6p8s4: check out octo-org/quiet and add notes';
const ANSWER = 'The notes are written kz40-answer-h8c3v6n1.';
const NOTES = '# Notes\nkz40-file-r5t9w2b6\n';
const CANARIES = [LOGIN, 'kz40-prompt-d2k6p8s4', 'kz40-answer-h8c3v6n1', 'kz40-file-r5t9w2b6', 'kz40-name-f4j8u1y5'];
/** 30 directories of 100 files each: 3000 files on `main`, fixed names and contents. */
const DIRS = 30;
const PER_DIR = 100;
const FILES = DIRS * PER_DIR;
const SCAN_SECONDS = 5; // agent_guest.scanner.SCAN_SECONDS
const QUIET_PASSES = 4;

function tree(): Record<string, string> {
  const files: Record<string, string> = {};
  for (let d = 0; d < DIRS; d += 1) {
    for (let f = 0; f < PER_DIR; f += 1) {
      const name = `src/pkg-${String(d).padStart(2, '0')}/mod-${String(f).padStart(3, '0')}.txt`;
      files[name] = `module ${d}.${f}\n`;
    }
  }
  return files;
}

async function sessionFiles(orchestrator: Orchestrator, session: string): Promise<number> {
  const rows = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.event WHERE session_id = '${uuid(session)}' AND kind = 'session.file'`,
  );
  return rows[0]!.n;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-40 @R-6.3 @R-P.5 @needs-vm a checkout of 3000 files adds no file change; the session write shows once with its patch', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const files = tree();
  expect(Object.keys(files)).toHaveLength(FILES);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(
    orchestrator,
    worldOf(LOGIN, 'octo-org', [
      repository(REPO, {
        branches: [{ name: 'main', commits: [{ message: 'Three thousand files.', files }] }],
      }),
    ]),
  );
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz40-name-f4j8u1y5');
  await connectGitHub(page, orchestrator, LOGIN);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  await allowRepositories(page, onboarded.org, org!.default_wg, [REPO]);

  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, ANSWER);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);

  // The checkout succeeded and imported all 3000 files into the workspace.
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.type, op.state])).toEqual([['github.checkout', 'succeeded']]);
  const [result] = await taskBodies(page, orchestrator, onboarded, task, 'operation.result');
  expect(result).toMatchObject({ status: 'succeeded', result: { repo: REPO, dir: DIR, files: FILES } });

  // The session's write reached the hub; then several scan passes of the guest go by and add nothing: the imported
  // tree is the scanner's baseline, not a change of the session (a regression reports it within one pass).
  await expect.poll(() => sessionFiles(orchestrator, session!.id), { timeout: 60_000 }).toBeGreaterThanOrEqual(1);
  for (let pass = 0; pass < QUIET_PASSES; pass += 1) {
    await page.waitForTimeout(SCAN_SECONDS * 1000);
    expect(await sessionFiles(orchestrator, session!.id)).toBe(1);
  }

  // The decrypted timeline holds exactly the session's own write, with its patch.
  const changes = await taskBodies(page, orchestrator, onboarded, task, 'session.file');
  expect(changes).toEqual([
    expect.objectContaining({
      path: `${DIR}/NOTES.md`,
      op: 'write',
      size: NOTES.length,
      sha256: createHash('sha256').update(NOTES).digest('hex'),
      patch: expect.stringContaining('+kz40-file-r5t9w2b6'),
    }),
  ]);

  // The chat shows one file change, none of the checked-out files, and the patch expands.
  const item = page.getByTestId('chat.file');
  await expect(item).toHaveCount(1);
  await expect(item).toHaveAttribute('data-op', 'write');
  await expect(page.getByTestId('chat.file.path')).toHaveText([`${DIR}/NOTES.md`]);
  await expect(page.getByTestId('chat.file.path').filter({ hasText: /src\/pkg-/ })).toHaveCount(0);
  await expect(page.getByTestId('chat.file.patch')).toBeHidden();
  await item.locator('summary').click();
  await expect(page.getByTestId('chat.file.patch')).toBeVisible();
  await expect(page.getByTestId('chat.file.patch')).toContainText('+kz40-file-r5t9w2b6');

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});
