// E2E-67 (KOR-45, ADR-087, ADR-088; 03-api.md sections 8, 8.1, 9; 07-testing.md section 2.1): sessions read like
// their human, and subagents are full sessions within limits that are settings.
// - Reads across workgroups: Ada (owner, explicit write access to the workgroups A and B) has a task and a whiteboard
//   entry in B; the platform device serves both workgroups (assigned to each when the first task there is sent). Her
//   session in A, on her instruction, lists B as readable (`my_access` read), lists B's tasks, reads B's whiteboard,
//   task and messages. Every read is audited as `read.session.<tool>` naming the workgroup read (`read_wg_id` B) and
//   the instructing human (`triggered_by` Ada).
// - The attack: Bob (member, write in A, no access to B) instructs the same session to read B. B is listed but not
//   readable (`my_access` null), its tasks and its task are `not_found`, audited as refused with `triggered_by` Bob.
//   The case of another owner without a right on B is the same rule (the readers are owner and instructing human,
//   both must read B), so one person without the right suffices.
// - Full subagents: the session requests a child and gives it three follow-ups with `subagent.instruct`; the child
//   answers each in a turn of its own (its requests carry its earlier turns), stays `idle` between them, the parent
//   reads each answer with `get_subagent_result` (the `instruction_id` of the instruction it answers) and gets one
//   `subagent.finished` ping per turn; `subagent.stop` ends it (`stopped`).
// - Limits as settings: the hub runs with a running limit of 1 (orchestrator route, restored at the end); a second
//   child while the first is alive ends `limit_exceeded` (runner pre-check `rejected` or hub `denied`) with the
//   sentence naming the setting `subagent_max_running`; the task's attention record is `blocked/limit_reached`.
// - No canary reaches the hub in plaintext. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, sessionState } from '../fixtures/guest.ts';
import { hubOperations } from '../fixtures/github.ts';
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
import { inviteMember, joinAsNewUser } from '../fixtures/team.ts';
import { boardEvent, navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { uuid7 } from '../../../web/src/core/encoding/ids';
import type { Page } from '@playwright/test';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-67-read-like-their-human.json', import.meta.url), 'utf8'),
);
const ADA_NAME = 'Ada kz67-name-a5u1';
const BOB_NAME = 'Bob kz67-name-b8i4';
const WG_A = 'Alpha kz67-wg-a-l3p9';
const WG_B = 'Beta kz67-wg-b-q7m2';
const B_PROMPT = 'E2E67-WGB kz67-prompt-b-k2d9: keep the budget figures of this workgroup';
const B_NOTE = 'Budget note kz67-board-b-x5n1';
const READ = 'E2E67-READ kz67-prompt-r-e1o5: what do the other workgroups know about the budget?';
const BOB = 'E2E67-BOB kz67-prompt-o-t6y2: read the tasks of the other workgroup as well';
const SUB = 'E2E67-SUB kz67-prompt-s-n4m8: let a helper keep a running sum';
const STOP = 'E2E67-STOP kz67-prompt-x-w9q3: the helper is no longer needed';
const CANARIES = [
  'kz67-name-a5u1',
  'kz67-name-b8i4',
  'kz67-wg-a-l3p9',
  'kz67-wg-b-q7m2',
  'kz67-prompt-b-k2d9',
  'kz67-board-b-x5n1',
  'kz67-prompt-r-e1o5',
  'kz67-prompt-o-t6y2',
  'kz67-prompt-s-n4m8',
  'kz67-prompt-x-w9q3',
  'kz67-purpose-s3f7',
  'kz67-instr-0-a1z4',
  'kz67-instr-1-b2y5',
  'kz67-instr-2-c3x6',
  'kz67-instr-3-d4w7',
  'kz67-child-answer-0',
  'kz67-child-answer-1',
  'kz67-child-answer-2',
  'kz67-child-answer-3',
  'kz67-answer-b-v3c8',
  'kz67-answer-read-h4j6',
  'kz67-answer-bob-r8w2',
  'kz67-answer-limit-p9e3',
  'kz67-answer-stop-g6t2',
];
/** The turns of the child: the request (instruction 0) and three follow-ups. */
const TURNS = 4;
/** The tools whose reads of workgroup B Ada's instruction allows (ADR-087 section 3). */
const B_READS = ['list_tasks', 'get_whiteboard', 'get_task', 'get_task_messages'];

interface JournalRecord {
  readonly path: string;
  readonly step?: string | null;
  readonly body: { readonly messages?: readonly { role: string; content: unknown }[] } | null;
}

interface ToolAnswer {
  readonly name: string;
  readonly input: Record<string, unknown>;
  readonly error: boolean;
  readonly value: Record<string, unknown>;
}

interface Workgroup {
  readonly id: string;
  readonly my_access: string | null;
}

interface SubagentResult {
  readonly session_id: string;
  readonly state: string;
  readonly final_text?: string;
  readonly instruction_id?: string;
}

/** Every platform tool call of the conversation the double answered with `step`, with its decoded answer, in order. */
async function platformAnswers(orchestrator: Orchestrator, step: string): Promise<ToolAnswer[]> {
  const { body } = await orchestrator.doubleRequest('model', 'GET', '/_double/journal');
  const records = (Array.isArray(body) ? body : []) as JournalRecord[];
  const found = records.filter((record) => record.path === '/v1/messages' && record.step === step).at(-1);
  expect(found, `no request answered by step ${step}`).toBeDefined();
  const calls = new Map<string, { name: string; input: Record<string, unknown> }>();
  const out: ToolAnswer[] = [];
  for (const message of found!.body?.messages ?? []) {
    if (!Array.isArray(message.content)) continue;
    for (const block of message.content as Record<string, unknown>[]) {
      if (block.type === 'tool_use' && String(block.name).startsWith('mcp__platform__'))
        calls.set(String(block.id), {
          name: String(block.name).slice('mcp__platform__'.length),
          input: block.input as Record<string, unknown>,
        });
      const call = block.type === 'tool_result' ? calls.get(String(block.tool_use_id)) : undefined;
      if (call === undefined) continue;
      const content = block.content;
      const text = typeof content === 'string' ? content : (content as { text: string }[]).map((c) => c.text).join('');
      out.push({ ...call, error: block.is_error === true, value: JSON.parse(text) as Record<string, unknown> });
    }
  }
  return out;
}

async function followUp(page: Page, text: string): Promise<void> {
  await page.getByTestId('chat.composer.prompt').fill(text);
  await page.getByTestId('chat.composer.send').click();
}

/** Sends `prompt` from the home composer in workgroup `wg`; returns the task. */
async function promptIn(page: Page, wg: string, prompt: string): Promise<string> {
  await home(page);
  const select = page.getByTestId('home.wg');
  await expect(select.locator(`option[value="${wg}"]`)).toHaveCount(1, { timeout: 30_000 });
  await select.selectOption(wg);
  return sendPrompt(page, prompt);
}

const setLimits = (orchestrator: Orchestrator, limits: Record<string, number>) =>
  orchestrator.call<Record<string, number | null>>('POST', '/hub/subagent-limits', limits);

test.describe.configure({ mode: 'serial' });

test('@E2E-67 @R-8.7 @R-8.8 @R-8.9 @R-13.4 @R-13.8 @needs-vm a session reads what its owner and its instructing human may both read, and its subagents take follow-ups until stopped within limits that are settings', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-67 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(1_500_000);
  // The hub restarts with the running limit 1 before anything else runs (the runners reconnect and take it with the
  // device state); every other limit keeps its default.
  expect(await setLimits(orchestrator, { running: 1 })).toMatchObject({ subagent_max_running: 1 });
  try {
    await orchestrator.loadScenario('model', scenario);
    const device = await platformDevice(orchestrator);
    await poolReady(orchestrator, 2);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, ADA_NAME);
    const org = onboarded.org;
    const ada = await PageUser.open(page, onboarded.recoveryCode);

    // Two workgroups with Ada's explicit write access; Bob writes in A and has no access to B.
    const wgA = await ada.createWorkgroup(org, WG_A);
    const wgB = await ada.createWorkgroup(org, WG_B);
    const bob = await joinAsNewUser(browser, e2e, org, await inviteMember(page, org, { [wgA]: 'write' }), BOB_NAME);
    const bobId = bob.user.userId;
    let log = await ada.log(org);
    expect([log.state.wgAccess(ada.userId, wgA), log.state.wgAccess(ada.userId, wgB)]).toEqual(['write', 'write']);
    expect([log.state.wgAccess(bobId, wgA), log.state.wgAccess(bobId, wgB)]).toEqual(['write', null]);

    // Workgroup B: a task (its title is the prompt's first line) and a whiteboard entry.
    const taskB = await promptIn(page, wgB, B_PROMPT);
    await expectAnswerInChat(page, 'Budget figures noted kz67-answer-b-v3c8.');
    log = await ada.log(org);
    const event = await boardEvent(ada, log, { org, wg: wgB, task: null }, 'whiteboard.entry', uuid7(), {
      level: 'workgroup',
      text: B_NOTE,
    });
    await ada.request('POST', '/v1/events', { events: [{ event }] });

    // 1. Ada's session in A reads B on Ada's instruction.
    const taskA = await promptIn(page, wgA, READ);
    await expectAnswerInChat(page, 'Workgroup B read for Ada kz67-answer-read-h4j6.');
    log = await ada.log(org);
    expect([log.state.assigned(device, wgA), log.state.assigned(device, wgB)]).toEqual([true, true]);
    const [main] = await mainSessions(orchestrator, taskA);

    // 2. Bob instructs the same session to read B (the session is idle, so the turn has his instruction only).
    await navigateInApp(bob.page, `/o/${org}/t/${taskA}`);
    await followUp(bob.page, BOB);
    await expectAnswerInChat(bob.page, 'Workgroup B is not readable for this instruction kz67-answer-bob-r8w2.');

    // 3. and 4. Ada's next instruction: a child with three follow-ups, then a second child over the running limit.
    await followUp(page, SUB);
    await expect(
      page
        .getByTestId('chat.output')
        .filter({ hasText: 'The limit of running helpers is reached kz67-answer-limit-p9e3.' }),
    ).toBeVisible({ timeout: 480_000 });
    const children = await orchestrator.hubQuery<{ id: string; parent_session_id: string; state: string }>(
      `SELECT id, parent_session_id, state FROM hub.session WHERE task_id = '${uuid(taskA)}' AND depth = 1`,
    );
    expect(children.map((row) => row.parent_session_id)).toEqual([main!.id]);
    const child = children[0]!.id;
    // after four turns the child still lives (no one-shot end): idle, waiting for the next instruction
    await expect.poll(async () => sessionState(orchestrator, child), { timeout: 30_000 }).toBe('idle');
    let operations = (await hubOperations(orchestrator, taskA)).filter((op) => op.type.startsWith('subagent.'));
    const refused = operations.find((op) => op.type === 'subagent.request' && op.error_code !== null);
    expect(refused).toBeDefined();
    expect(['rejected', 'denied']).toContain(refused!.state);
    expect(refused!.error_code).toBe('limit_exceeded');
    await expect
      .poll(
        async () =>
          (
            await orchestrator.hubQuery<{ category: string; reason: string; ref_id: string | null }>(
              `SELECT category, reason, ref_id::text FROM hub.task_attention WHERE task_id = '${uuid(taskA)}'`,
            )
          )[0],
        { timeout: 60_000 },
      )
      .toEqual({ category: 'blocked', reason: 'limit_reached', ref_id: refused!.id });

    // 3. (end) Ada's last instruction stops the child.
    await followUp(page, STOP);
    await expectAnswerInChat(page, 'The helper is stopped kz67-answer-stop-g6t2.');
    await expect.poll(async () => sessionState(orchestrator, child), { timeout: 60_000 }).toBe('stopped');
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    operations = (await hubOperations(orchestrator, taskA)).filter((op) => op.type.startsWith('subagent.'));
    expect(operations.map((op) => [op.type, op.state === 'succeeded' ? 'succeeded' : op.error_code])).toEqual([
      ['subagent.request', 'succeeded'],
      ['subagent.instruct', 'succeeded'],
      ['subagent.instruct', 'succeeded'],
      ['subagent.instruct', 'succeeded'],
      ['subagent.request', 'limit_exceeded'],
      ['subagent.stop', 'succeeded'],
    ]);
    expect(
      (await orchestrator.hubQuery(`SELECT id FROM hub.session WHERE task_id = '${uuid(taskA)}' AND depth = 1`)).length,
    ).toBe(1);

    // The answers the session got, in call order over all its turns.
    const answers = await platformAnswers(orchestrator, 'stop-done');
    const byAda = answers.slice(0, 5);
    const byBob = answers.slice(5, 8);
    const subagents = answers.slice(8);
    expect(byAda.map((answer) => answer.name)).toEqual(['list_workgroups', ...B_READS]);
    expect(byBob.map((answer) => answer.name)).toEqual(['list_workgroups', 'list_tasks', 'get_task']);

    // 1. What Ada may read: B readable, its task with its title, its whiteboard entry, its task and prompt.
    expect(byAda.filter((answer) => answer.error)).toEqual([]);
    const listed = (index: number, wg: string) =>
      (byAda.concat(byBob)[index]!.value.workgroups as Workgroup[]).find((item) => item.id === wg);
    expect(listed(0, wgB)?.my_access).toBe('read');
    expect(listed(0, wgA)?.my_access).toBe('write');
    expect(byAda[1]!.input).toEqual({ workgroup_id: wgB });
    expect((byAda[1]!.value.tasks as { id: string; title: string }[]).map((task) => [task.id, task.title])).toEqual([
      [taskB, B_PROMPT],
    ]);
    expect(byAda[2]!.input).toEqual({ level: 'workgroup', workgroup_id: wgB });
    expect(
      (byAda[2]!.value.entries as { text: string; author_id: string }[]).map((e) => [e.text, e.author_id]),
    ).toEqual([[B_NOTE, ada.userId]]);
    expect(byAda[3]!.value).toMatchObject({ id: taskB });
    expect((byAda[4]!.value.messages as { kind: string; text: string }[])[0]).toMatchObject({
      kind: 'prompt',
      text: B_PROMPT,
    });

    // 2. What Bob may not read through Ada's session: B listed by name only, its tasks and its task not found.
    expect(listed(5, wgB)?.my_access).toBeNull();
    for (const answer of byBob.slice(1)) {
      expect(answer.error).toBe(true);
      expect(answer.value).toMatchObject({ error: 'not_found' });
    }
    expect(byBob[1]!.input).toEqual({ workgroup_id: wgB });
    expect(byBob[2]!.input).toEqual({ task_id: taskB });

    // The read log: the workgroup read and the instructing human of every read (ADR-087 section 4).
    const reads = (await auditRows(orchestrator, taskA)).filter(
      (row) => row.action.startsWith('read.session.') && row.session_id === main!.id,
    );
    const detail = (row: (typeof reads)[number]) => row.detail as { read_wg_id?: string | null; triggered_by?: string };
    const adaReads = reads.filter((row) => detail(row).triggered_by === ada.userId);
    for (const tool of B_READS) {
      const row = adaReads.find((item) => item.action === `read.session.${tool}`);
      expect(row, `audit of ${tool} on Ada's instruction`).toBeDefined();
      expect([row!.result, detail(row!).read_wg_id]).toEqual(['ok', wgB]);
    }
    const bobReads = reads.filter((row) => detail(row).triggered_by === bobId);
    expect(bobReads.map((row) => row.action).sort()).toEqual(
      ['read.session.get_task', 'read.session.list_tasks', 'read.session.list_workgroups'].sort(),
    );
    const bobList = bobReads.find((row) => row.action === 'read.session.list_tasks')!;
    expect(['denied', 'rejected']).toContain(bobList.result);
    expect(detail(bobList).read_wg_id).toBe(wgB);
    expect(['denied', 'rejected']).toContain(bobReads.find((row) => row.action === 'read.session.get_task')!.result);

    // 3. The child's turns: one instruction per turn, each answer read with the ID of its instruction.
    const requested = subagents.find((answer) => answer.name === 'request_operation')!;
    expect(requested.value).toMatchObject({ status: 'succeeded', result: { session_id: child } });
    const instructions = [String(requested.value.operation_id)];
    for (const answer of subagents.filter(
      (item) => item.name === 'request_operation' && item.input.type === 'subagent.instruct',
    )) {
      expect(answer.value).toMatchObject({ status: 'succeeded', result: { session_id: child } });
      instructions.push(String((answer.value.result as { instruction_id: string }).instruction_id));
    }
    expect(new Set(instructions).size).toBe(TURNS);
    const results = subagents
      .filter((answer) => answer.name === 'get_subagent_result')
      .map((answer) => answer.value as unknown as SubagentResult);
    expect(results.every((result) => result.session_id === child)).toBe(true);
    for (let turn = 0; turn < TURNS; turn += 1) {
      const answered = results.filter(
        (result) =>
          ['idle', 'suspended'].includes(result.state) && result.final_text?.includes(`kz67-child-answer-${turn}`),
      );
      expect(answered.length, `answer of instruction ${turn}`).toBeGreaterThan(0);
      for (const result of answered) expect(result.instruction_id).toBe(instructions[turn]);
    }
    const last = results.at(-1)!;
    expect(last.state).toBe('stopped');
    expect(last.final_text).toContain('kz67-child-answer-3');
    // one subagent.finished ping per turn (get_notifications hands every ping once)
    const pings = subagents
      .filter((answer) => answer.name === 'get_notifications')
      .flatMap((answer) => answer.value.notifications as { ping_id: string; kind: string; ref: string }[])
      .filter((ping) => ping.kind === 'subagent.finished' && ping.ref === child);
    expect(new Set(pings.map((ping) => ping.ping_id)).size).toBeGreaterThanOrEqual(TURNS);

    // 4. The clear message of the limit: its value and its setting.
    const second = subagents.filter((answer) => answer.name === 'request_operation').at(-2)!;
    expect(second.input.type).toBe('subagent.request');
    expect(['rejected', 'denied']).toContain(second.value.status);
    const error = second.value.error as { code: string; message: string };
    expect(error.code).toBe('limit_exceeded');
    expect(error.message).toContain('1 running subagent');
    expect(error.message).toContain('(setting subagent_max_running)');

    // 5. Zero knowledge at the hub.
    await expectNoPlaintext(orchestrator, CANARIES);
    await bob.page.context().close();
  } finally {
    await setLimits(orchestrator, {});
  }
});
