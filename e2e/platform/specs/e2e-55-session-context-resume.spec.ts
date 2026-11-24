// E2E-55 session context resume (KOR-33; ADR-072; concept ideas 39, 40, 43): the original Claude Code session
// continues with its whole conversation after the task's main session ended - "Close task" with the VM deleted
// afterwards, "End session", a failure of the agent, and the guest's 7-day retention - in a NEW main session row that
// runs `claude --resume <the same claude session id>` from the encrypted context checkpoint: the model double sees
// the earlier turn in the request (its journal holds the whole request), the restored workspace file is read from the
// new work directory, the restore adds no `session.file` record (HOT-06 baseline rule) and the task shows the
// "restored" line. The emergency path (the hub's checkpoint damaged or deleted, no copy on the device) starts a new
// conversation, says why on the task and writes `session.context_fallback`; an upload interrupted before its commit
// is never restored, the complete older checkpoint is. The hub side holds none of the texts (plaintext scan). Runs on
// T1. Hooks: orchestrator/routes/session_context.py.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import {
  auditRows,
  expectNoPlaintext,
  osRead,
  runnerStore,
  sessionState,
  sessionVm,
  vmFiles,
} from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
  type Onboarded,
} from '../fixtures/platform.ts';
import type { Page } from '@playwright/test';

const load = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));
const RESUME = load('e2e-55-session-context-resume');
const FALLBACK = load('e2e-55-context-fallback');
const INTERRUPTED = load('e2e-55-interrupted-upload');

const PROMPT = 'E2E40-WORK kz40r-prompt-s4h8d2f6: start a ledger in ledger.md';
const FOLLOW_UP = 'E2E40-AGAIN kz40r-follow-y3n7c5a1: what does the ledger hold?';
const MORE = 'E2E40-MORE kz40r-more-q9b2k6t3: also write second.md';
const NAME = 'Ada kz40r-name-e1x6w9p2';
const FILE_CANARY = 'kz40r-file-p3w8n5c1';
const ANSWER = 'The ledger is written kz40r-answer-g6t2m9x4.';
const SECOND_ANSWER = 'The second file is written kz40r-second-answer-u5e8h3w6.';
const CONTINUED = 'Continued: the ledger still holds kz40r-file-p3w8n5c1.';
const FRESH = 'A new conversation kz40r-fresh-b7q1z5k8.';
const CANARIES = [
  'kz40r-prompt-s4h8d2f6',
  'kz40r-follow-y3n7c5a1',
  'kz40r-name-e1x6w9p2',
  FILE_CANARY,
  'kz40r-answer-g6t2m9x4',
  'E2E40-WORK',
];
const SCAN_SECONDS = 5; // agent_guest.scanner.SCAN_SECONDS
const START_WINDOW_MS = 12_000;
const CHECKPOINT_TIMEOUT = 240_000;
const RESTORED_TEXT = 'Continued with the full context of the original session';

interface First {
  readonly onboarded: Onboarded;
  readonly task: string;
  readonly session: string;
  readonly vm: string;
  readonly claude: string;
}

interface JournalRecord {
  readonly path: string;
  readonly step?: string | null;
  readonly body: unknown;
}

const post = <T>(orchestrator: Orchestrator, path: string, body: unknown = {}) =>
  orchestrator.call<T>('POST', path, body);

/** The Claude Code session ID the runner recorded for `session` (null until the guest reported it). */
async function claudeId(orchestrator: Orchestrator, session: string): Promise<string | null> {
  const rows = await runnerStore<{ claude_session_id: string | null }>(
    orchestrator,
    `SELECT claude_session_id FROM runner.session WHERE id = '${uuid(session)}'`,
  );
  return rows[0]?.claude_session_id ?? null;
}

/** The complete checkpoints of the task at the hub, oldest first (the hub's order within one session). */
async function completeObjects(orchestrator: Orchestrator, task: string): Promise<string[]> {
  const rows = await orchestrator.hubQuery<{ id: string }>(
    `SELECT o.id FROM hub.context_object o JOIN hub.session s ON s.id = o.session_id
      WHERE o.task_id = '${uuid(task)}' AND o.state = 'complete' ORDER BY s.created_at, o.id`,
  );
  return rows.map((row) => row.id);
}

/**
 * Waits until the runner has nothing due for `session` and every local checkpoint of it is committed at the hub, and
 * the hub holds at least `minimum` complete checkpoints of the task. Returns the hub's complete ones.
 */
async function checkpointed(orchestrator: Orchestrator, task: string, session: string, minimum = 1): Promise<string[]> {
  await expect
    .poll(
      async () => {
        const [due] = await runnerStore<{ due: boolean; open: number }>(
          orchestrator,
          `SELECT (SELECT count(*) FROM runner.context_session WHERE session_id = '${uuid(session)}'
                     AND due_at IS NOT NULL) > 0 AS due,
                  (SELECT count(*)::int FROM runner.context_object WHERE session_id = '${uuid(session)}'
                     AND state <> 'committed') AS open`,
        );
        const complete = (await completeObjects(orchestrator, task)).length;
        return !due!.due && due!.open === 0 && complete >= minimum;
      },
      { timeout: CHECKPOINT_TIMEOUT, intervals: [2000] },
    )
    .toBe(true);
  return completeObjects(orchestrator, task);
}

/** The newest request the model double answered with step `step` (its journal keeps the whole request body). */
async function requestOf(orchestrator: Orchestrator, step: string): Promise<string> {
  const { body } = await orchestrator.doubleRequest('model', 'GET', '/_double/journal');
  const records = (Array.isArray(body) ? body : []) as JournalRecord[];
  const found = records.filter((record) => record.path === '/v1/messages' && record.step === step).at(-1);
  expect(found, `no request answered by step ${step}`).toBeDefined();
  return JSON.stringify(found!.body);
}

async function sessionFileAuthors(orchestrator: Orchestrator, task: string): Promise<string[]> {
  const rows = await orchestrator.hubQuery<{ session_id: string }>(
    `SELECT session_id FROM hub.event WHERE task_id = '${uuid(task)}' AND kind = 'session.file' ORDER BY scope_seq`,
  );
  return rows.map((row) => row.session_id);
}

/** Onboarding, the first turn (ledger.md written, answer shown) and the IDs the later steps compare with. */
async function firstTurn(
  page: Page,
  orchestrator: Orchestrator,
  scenario: unknown,
  authenticator: () => Promise<unknown>,
): Promise<First> {
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator();
  const onboarded = await onboard(page, orchestrator, NAME);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, ANSWER);
  const [main] = await mainSessions(orchestrator, task);
  const session = main!.id;
  await expect.poll(() => sessionFileAuthors(orchestrator, task), { timeout: 60_000 }).toEqual([session]);
  await expect.poll(() => claudeId(orchestrator, session), { timeout: 60_000 }).toMatch(/^[0-9a-f-]{36}$/);
  const claude = (await claudeId(orchestrator, session))!;
  const { vm } = await sessionVm(orchestrator, session);
  return { onboarded, task, session, vm, claude };
}

async function followUp(page: Page, text: string): Promise<void> {
  await page.getByTestId('chat.composer.prompt').fill(text);
  await page.getByTestId('chat.composer.send').click();
}

async function endSession(page: Page): Promise<void> {
  await page.getByTestId('chat.control.menu').locator('summary').click();
  await page.getByTestId('chat.control.end').click();
}

async function waitState(orchestrator: Orchestrator, session: string, state: string): Promise<void> {
  await expect.poll(() => sessionState(orchestrator, session), { timeout: 120_000, intervals: [1000] }).toBe(state);
}

/**
 * The follow-up continued the original conversation in a second main session: same Claude session ID with
 * `--resume`, the earlier turn in the model's request, the restored ledger.md readable in the new sandbox, the hub
 * row and audit say `restored`, the task shows the restored line, and the restore added no `session.file` record.
 * Returns the second session and the checkpoint it started from.
 */
async function expectRestored(
  page: Page,
  orchestrator: Orchestrator,
  first: First,
  count = 2,
): Promise<{ session: string; vm: string; object: string }> {
  await expectAnswerInChat(page, CONTINUED);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const sessions = await mainSessions(orchestrator, first.task);
  expect(sessions.map((row) => row.id).slice(0, 1)).toEqual([first.session]);
  expect(sessions).toHaveLength(count);
  const second = sessions.at(-1)!.id;

  // The same Claude Code conversation: `--resume <claude session id of the first session>`, the same ID recorded.
  const running = await sessionVm(orchestrator, second);
  expect(running.command.indexOf('--resume'), running.command.join(' ')).toBeGreaterThan(0);
  expect(running.command[running.command.indexOf('--resume') + 1]).toBe(first.claude);
  await expect.poll(() => claudeId(orchestrator, second), { timeout: 30_000 }).toBe(first.claude);

  // The model saw the whole earlier turn (prompt, its tool call with the file content, the tool result, the answer),
  // not a summary.
  const request = await requestOf(orchestrator, 'follow-read');
  for (const earlier of [PROMPT, 'I will keep the ledger first.', FILE_CANARY, ANSWER, FOLLOW_UP]) {
    expect(request, `the resumed request lacks ${earlier}`).toContain(earlier);
  }
  // Claude Code keeps the system prompt stored in the transcript on `--resume`, so the change reaches the model as
  // the guest's session notice on the first hook answer (hook_addons/notice.py): what changed and the current
  // platform session ID, inside the same conversation.
  expect(request, 'the resumed request lacks the session notice').toContain('agent.dfx.swiss session notice: ');
  expect(request, 'the session notice lacks the new session ID').toContain(second);

  // The restored workspace file is readable by the new session in its sandbox.
  const read = await osRead(orchestrator, second, `/srv/sessions/${second}/work/ledger.md`);
  expect(read.in_sandbox).toMatchObject({ ok: true });
  expect(read.in_sandbox.data).toContain(FILE_CANARY);

  // Hub row and audit: restored from a checkpoint of the task, the whole workspace.
  const [row] = await orchestrator.hubQuery<{ context_restore: string | null; context_reason: string | null }>(
    `SELECT context_restore, context_reason FROM hub.session WHERE id = '${uuid(second)}'`,
  );
  expect(row).toEqual({ context_restore: 'restored', context_reason: null });
  const audits = (await auditRows(orchestrator, first.task, 'session.context_restored')).filter(
    (audit) => audit.session_id === second,
  );
  expect(audits).toHaveLength(1);
  expect(audits[0]!.detail).toMatchObject({ workspace: 'full', stale: false });
  const object = String(audits[0]!.detail.object_id);
  expect(await auditRows(orchestrator, first.task, 'session.context_fallback')).toEqual([]);

  // The task screen shows the unobtrusive restored line, no fallback warning.
  await expect(page.getByTestId('task.context.restored')).toHaveText(RESTORED_TEXT, { timeout: 30_000 });
  await expect(page.getByTestId('task.context.restored')).toHaveAttribute('data-workspace', 'full');
  await expect(page.getByTestId('task.context.fallback')).toHaveCount(0);

  // Several scan passes of the guest go by: the restored tree is the scanner's baseline, the restored session has no
  // file change (the first session's own write stays the only record of ledger.md).
  await page.waitForTimeout(3 * SCAN_SECONDS * 1000);
  const authors = await sessionFileAuthors(orchestrator, first.task);
  expect(authors[0]).toBe(first.session);
  expect(authors.filter((author) => author === second)).toEqual([]);
  await expect(page.getByTestId('chat.file')).toHaveCount(authors.length);
  return { session: second, vm: running.vm, object };
}

test.describe.configure({ mode: 'serial' });

test('@E2E-55 @R-6.3 @needs-vm after "Close task" and the deletion of its VM a follow-up continues the original session on another VM', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const first = await firstTurn(page, orchestrator, RESUME, () => authenticator('platform'));

  await page.getByTestId('task.close').click();
  await page.getByTestId('task.close.confirm').click();
  await expect(page.getByTestId('task.state')).toHaveAttribute('data-state', 'closed');
  await waitState(orchestrator, first.session, 'stopped');
  await checkpointed(orchestrator, first.task, first.session);

  // The VM disappears (deleted by hand under its runner); the device keeps its encrypted checkpoint.
  await post(orchestrator, `/vms/${first.vm}/delete`);
  expect(Object.keys(await vmFiles(orchestrator, ['/srv/sessions']))).not.toContain(first.vm);

  await followUp(page, FOLLOW_UP);
  const second = await expectRestored(page, orchestrator, first);
  expect(second.vm).not.toBe(first.vm);
  await expect(page.getByTestId('task.state')).not.toHaveAttribute('data-state', 'closed');
  await expectNoPlaintext(orchestrator, CANARIES);
});

test('@E2E-55 @R-6.3 @needs-vm after "End session" a follow-up continues the original session on the same VM', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const first = await firstTurn(page, orchestrator, RESUME, () => authenticator('platform'));
  await endSession(page);
  await waitState(orchestrator, first.session, 'stopped');
  await checkpointed(orchestrator, first.task, first.session);

  await followUp(page, FOLLOW_UP);
  const second = await expectRestored(page, orchestrator, first);
  expect(second.vm).toBe(first.vm);
  await expectNoPlaintext(orchestrator, CANARIES);
});

test('@E2E-55 @R-6.3 @needs-vm after the agent failed a follow-up continues the original session', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const first = await firstTurn(page, orchestrator, RESUME, () => authenticator('platform'));
  await waitState(orchestrator, first.session, 'idle');
  // Claude Code dies (SIGKILL) after the guest's start window (agent_guest.sessions.START_WINDOW_SECONDS, 10 s): the
  // guest ends the session `agent_exited`.
  await page.waitForTimeout(START_WINDOW_MS);
  await post(orchestrator, `/sessions/${first.session}/agent-kill`);
  await waitState(orchestrator, first.session, 'failed');
  const [failed] = await orchestrator.hubQuery<{ state_reason: string }>(
    `SELECT state_reason FROM hub.session WHERE id = '${uuid(first.session)}'`,
  );
  expect(failed!.state_reason).toBe('agent_exited');
  await checkpointed(orchestrator, first.task, first.session);

  await followUp(page, FOLLOW_UP);
  await expectRestored(page, orchestrator, first);
  await expectNoPlaintext(orchestrator, CANARIES);
});

test('@E2E-55 @R-6.3 @needs-vm after "8 days" (guest retention removed the session, no copy on the device) the hub checkpoint restores the session', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  const first = await firstTurn(page, orchestrator, RESUME, () => authenticator('platform'));
  await endSession(page);
  await waitState(orchestrator, first.session, 'stopped');
  const atHub = await checkpointed(orchestrator, first.task, first.session);

  // The guest's 7-day retention removed transcript and workspace of the ended session in the VM ...
  const expired = await post<{ existed: boolean }>(orchestrator, `/vms/${first.vm}/sessions/${first.session}/expire`);
  expect(expired.existed).toBe(true);
  const facts = await vmFiles(orchestrator, [`/srv/sessions/${first.session}`]);
  expect(facts[first.vm]).toEqual({ [`/srv/sessions/${first.session}`]: null });
  // ... and the device holds no copy of the checkpoint: only the hub has it.
  const evicted = await post<{ objects: [string, string][] }>(orchestrator, '/runners/runner-1/contexts/evict', {
    task: first.task,
  });
  expect(evicted.objects.length).toBeGreaterThan(0);
  expect(evicted.objects.map(([, state]) => state)).toEqual(evicted.objects.map(() => 'committed'));

  await followUp(page, FOLLOW_UP);
  const second = await expectRestored(page, orchestrator, first);
  // The checkpoint came from the hub: it is the hub's newest complete one, whose local copy had been evicted (the
  // fetched copy is pruned from the cache again once the new session's own checkpoint is the task's newest).
  expect(second.object).toBe(atHub.at(-1));
  expect(evicted.objects.map(([object]) => object)).toContain(second.object);
  await expectNoPlaintext(orchestrator, CANARIES);
});

for (const [action, reason, text] of [
  ['corrupt', 'context_invalid', 'the saved context was damaged or could not be verified'],
  ['delete', 'context_missing', 'no saved context existed for the earlier session'],
] as const) {
  test(`@E2E-55 @R-6.3 @needs-vm a ${action === 'corrupt' ? 'damaged' : 'deleted'} hub checkpoint takes the reported emergency path (${reason})`, async ({
    page,
    orchestrator,
    authenticator,
  }) => {
    test.setTimeout(900_000);
    const first = await firstTurn(page, orchestrator, FALLBACK, () => authenticator('platform'));
    await endSession(page);
    await waitState(orchestrator, first.session, 'stopped');
    await checkpointed(orchestrator, first.task, first.session);
    const tampered = await post<{ objects: string[] }>(orchestrator, '/hub-db/contexts/tamper', {
      task: first.task,
      action,
    });
    expect(tampered.objects.length).toBeGreaterThan(0);
    await post(orchestrator, '/runners/runner-1/contexts/evict', { task: first.task });

    await followUp(page, FOLLOW_UP);
    await expectAnswerInChat(page, FRESH);
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    const sessions = await mainSessions(orchestrator, first.task);
    expect(sessions).toHaveLength(2);
    const second = sessions[1]!.id;

    // A new conversation: another Claude session ID, none of the earlier turn in the request.
    await expect.poll(() => claudeId(orchestrator, second), { timeout: 30_000 }).toMatch(/^[0-9a-f-]{36}$/);
    expect(await claudeId(orchestrator, second)).not.toBe(first.claude);
    // (The task title in the harness is the first prompt; only the conversation's messages are compared.)
    const messages = JSON.stringify(
      (JSON.parse(await requestOf(orchestrator, 'fresh')) as { messages: unknown }).messages,
    );
    for (const earlier of [PROMPT, FILE_CANARY, ANSWER]) expect(messages).not.toContain(earlier);
    expect(messages).toContain(FOLLOW_UP);

    // Reported, never silent: hub row, audit row and the warning on the task with the reason.
    const [row] = await orchestrator.hubQuery<{ context_restore: string | null; context_reason: string | null }>(
      `SELECT context_restore, context_reason FROM hub.session WHERE id = '${uuid(second)}'`,
    );
    expect(row).toEqual({ context_restore: 'fallback', context_reason: reason });
    const audits = await auditRows(orchestrator, first.task, 'session.context_fallback');
    expect(audits.map((audit) => [audit.session_id, audit.detail.reason])).toEqual([[second, reason]]);
    expect(await auditRows(orchestrator, first.task, 'session.context_restored')).toEqual([]);
    const notice = page.getByTestId('task.context.fallback');
    await expect(notice).toHaveAttribute('data-reason', reason, { timeout: 30_000 });
    await expect(notice).toHaveText(`This task was continued by a new AI because ${text}.`);
    await expect(page.getByTestId('task.context.restored')).toHaveCount(0);
    await expectNoPlaintext(orchestrator, CANARIES);
  });
}

test('@E2E-55 @R-6.3 @needs-vm an upload interrupted before its commit is never restored; the complete older checkpoint is', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  // Session 1: the first turn, then "End session": its final checkpoint is complete at the hub.
  const first = await firstTurn(page, orchestrator, INTERRUPTED, () => authenticator('platform'));
  await endSession(page);
  await waitState(orchestrator, first.session, 'stopped');
  const older = (await checkpointed(orchestrator, first.task, first.session)).at(-1)!;

  // Session 2 continues the conversation (restored) and writes second.md, then ends: its checkpoints are newer.
  await followUp(page, MORE);
  await expectAnswerInChat(page, SECOND_ANSWER);
  const [, middle] = await mainSessions(orchestrator, first.task);
  await endSession(page);
  await waitState(orchestrator, middle!.id, 'stopped');
  await checkpointed(orchestrator, first.task, middle!.id, 2);
  const newer = (
    await orchestrator.hubQuery<{ id: string }>(
      `SELECT id FROM hub.context_object WHERE session_id = '${uuid(middle!.id)}' AND state = 'complete'`,
    )
  ).map((row) => row.id);
  expect(newer.length).toBeGreaterThan(0);

  // Every checkpoint of session 2 becomes an upload without commit (chunks stored, the commit never arrived); the
  // device holds no copy, so only the hub can serve one: the newest complete checkpoint is session 1's final one.
  const uncommitted = await post<{ objects: string[] }>(orchestrator, '/hub-db/contexts/tamper', {
    task: first.task,
    action: 'uncommit',
    objects: newer,
  });
  expect(uncommitted.objects).toEqual([...newer].sort());
  expect((await completeObjects(orchestrator, first.task)).at(-1)).toBe(older);
  await post(orchestrator, '/runners/runner-1/contexts/evict', { task: first.task });

  await followUp(page, FOLLOW_UP);
  const third = await expectRestored(page, orchestrator, first, 3);
  expect(third.object).toBe(older);
  // The second turn is not part of the restored conversation, and its file is not in the restored workspace.
  const request = await requestOf(orchestrator, 'follow-read');
  for (const later of [MORE, 'kz40r-second-file-j2v7r4d9', SECOND_ANSWER]) expect(request).not.toContain(later);
  const facts = await vmFiles(orchestrator, [`/srv/sessions/${third.session}/work/second.md`], third.session);
  expect(Object.values(facts)).toEqual([{ [`/srv/sessions/${third.session}/work/second.md`]: null }]);
  await expectNoPlaintext(orchestrator, CANARIES);
});
