// E2E-10 (07-testing.md section 2.1; 05-runner.md sections 4.3, 4.4; ADR-008): isolated sandboxes in one VM. Two
// tasks of one workgroup run in parallel in the workgroup's VM: session B writes a secret and keeps working (its
// answer is held 30 s), while adversary session A tries to read, grep, list and write B's workspace. The PreToolUse
// policy denies every attempt (`hook.denied`); the OS layer refuses too: with A's identity the path does not exist
// in A's mount namespace and is unreadable even outside it (UID, 0700). A writes in its own workspace; both tasks
// complete; B's files are untouched. Runs on T1.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, osRead, sessionState, sessionVm, vmFiles } from '../fixtures/guest.ts';
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
import type { Orchestrator } from '../fixtures/orchestrator.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-10-isolated-sandboxes.json', import.meta.url), 'utf8'),
);
const PROMPT_B = 'E2E10-BRAVO kz10-prompt-b-f1d6s3a8: store your secret';
const SECRET = 'kz10-secret-b-w5n2q7z4\n';
const CANARIES = [
  'kz10-prompt-b-f1d6s3a8',
  'kz10-prompt-a-g9h4j2k7',
  'kz10-secret-b-w5n2q7z4',
  'kz10-alpha-own-p4l9',
  'kz10-answer-a-k2u7i5o3',
  'kz10-answer-b-e3r8t1y6',
  'kz10-name-m3n8b5v1',
];

async function sessionOf(orchestrator: Orchestrator, task: string): Promise<string> {
  let id = '';
  await expect
    .poll(async () => (id = (await mainSessions(orchestrator, task))[0]?.id ?? ''), { timeout: 30_000 })
    .not.toBe('');
  return id;
}

async function count(orchestrator: Orchestrator, session: string, kind: string): Promise<number> {
  const rows = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.event WHERE session_id = '${uuid(session)}' AND kind = '${kind}'`,
  );
  return rows[0]!.n;
}

test.describe.configure({ mode: 'serial' });

// E2E-10 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-10 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-10 @R-3.8 @R-3.9 @R-14.2 @R-P.5 @needs-vm two sessions of one workgroup share the VM in parallel and cannot reach each other', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz10-name-m3n8b5v1');

  // B writes its secret and keeps working (its next answer is held by the model double).
  const taskB = await sendPrompt(page, PROMPT_B);
  const sessionB = await sessionOf(orchestrator, taskB);
  await expect.poll(() => count(orchestrator, sessionB, 'session.file'), { timeout: 90_000 }).toBe(1);
  await expect
    .poll(async () => (await orchestrator.doubleState('model')).consumed, { timeout: 30_000 })
    .toContain('b-hold');
  const foreign = `/srv/sessions/${sessionB}/work`;

  // A in the same workgroup, in parallel: every attempt on B's workspace fails, its own write works.
  await home(page);
  const taskA = await sendPrompt(page, `E2E10-ALPHA kz10-prompt-a-g9h4j2k7: compare with ${foreign}`);
  const sessionA = await sessionOf(orchestrator, taskA);
  await expectAnswerInChat(page, 'Alpha finished kz10-answer-a-k2u7i5o3.');
  expect(await sessionState(orchestrator, sessionB)).toBe('working');

  const [vmA, vmB] = [await sessionVm(orchestrator, sessionA), await sessionVm(orchestrator, sessionB)];
  expect(vmA.vm).toBe(vmB.vm);
  expect(vmA.uid).not.toBe(vmB.uid);
  expect(vmA.user).not.toBe(vmB.user);
  expect([vmA.unit, vmB.unit]).toEqual(['active', 'active']);
  expect(vmA.main_pid).not.toBe(vmB.main_pid);

  // OS layer with A's identity: invisible inside A's sandbox, unreadable outside it; B itself can read it.
  const secretPath = `${foreign}/secret-b.txt`;
  const fromA = await osRead(orchestrator, sessionA, secretPath);
  expect(fromA.in_sandbox).toMatchObject({ ok: false, error: 'FileNotFoundError', data: '' });
  expect(fromA.as_user).toMatchObject({ ok: false, error: 'PermissionError', data: '' });
  const fromB = await osRead(orchestrator, sessionB, secretPath);
  expect(fromB.in_sandbox).toEqual({ ok: true, error: null, data: SECRET });

  // Hook layer: each attempt denied and audited (tool and rule, never the path).
  const tools = await taskBodies(page, orchestrator, onboarded, taskA, 'session.tool');
  // (a successful Write is reported by its PostToolUse hook, the refused calls from the transcript: compare as a set)
  const calls = tools.map((tool) => `${String(tool.name)}:${String(tool.is_error)}`).sort();
  expect(calls).toEqual(['Glob:true', 'Grep:true', 'Read:true', 'Write:false', 'Write:true']);
  for (const tool of tools) expect(String(tool.result_summary)).not.toContain('kz10-secret-b');
  const denied = (await taskBodies(page, orchestrator, onboarded, taskA, 'session.hook')).filter(
    (body) => body.event === 'PreToolUse' && body.decision === 'deny',
  );
  expect(denied.map((body) => [body.tool, body.rule])).toEqual([
    ['Read', 6],
    ['Grep', 6],
    ['Glob', 6],
    ['Write', 5],
  ]);
  const audit = await auditRows(orchestrator, taskA, 'hook.denied');
  expect(audit.map((row) => [row.session_id, row.detail.tool, row.detail.rule])).toEqual([
    [sessionA, 'Read', 6],
    [sessionA, 'Grep', 6],
    [sessionA, 'Glob', 6],
    [sessionA, 'Write', 5],
  ]);

  // Both complete; B's workspace holds only its own secret, A's file is in A's workspace.
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 90_000 }).toBe(true);
  await expect.poll(() => sessionState(orchestrator, sessionB), { timeout: 60_000 }).toBe('idle');
  const files = await vmFiles(
    orchestrator,
    [secretPath, `${foreign}/planted.txt`, `/srv/sessions/${sessionA}/work/alpha.txt`],
    sessionB,
  );
  const facts = Object.values(files)[0]!;
  expect(facts[secretPath]?.sha256).toBe(createHash('sha256').update(SECRET).digest('hex'));
  expect(facts[`${foreign}/planted.txt`]).toBeNull();
  expect(facts[`/srv/sessions/${sessionA}/work/alpha.txt`]).not.toBeNull();
  expect((await taskBodies(page, orchestrator, onboarded, taskB, 'session.output')).map((b) => b.text)).toEqual([
    'Storing the secret of B.',
    'Bravo finished kz10-answer-b-e3r8t1y6.',
  ]);

  await expectNoPlaintext(orchestrator, CANARIES);
});
