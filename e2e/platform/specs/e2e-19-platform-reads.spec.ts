// E2E-19 (07-testing.md section 2.1; 03-api.md sections 8, 8.1, 10; ADR-089): session platform reads. The session calls
// every MCP read tool; the answers match what the user sees in the web (workgroups, members, the task and its prompt,
// its own operation). A task of another workgroup of the same org is read by its ID: the session reads what its owner
// and the instructing person may both read (ADR-089), here the same person, the org's only member and an admin, who
// may read every workgroup, and the device serves that workgroup (the task ran there), so its task, messages and task
// whiteboard are answered and `list_workgroups` names it `read`. This org cannot express a workgroup the owner may not
// read; the `not_found` case (a member without the right instructs the session) is E2E-68. Every read is audited at the
// hub (`read.session.<tool>`, naming the workgroup read) and written as an encrypted `platform.read` event of the task
// (tool, arguments, result count). Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import { hubOperations } from '../fixtures/github.ts';
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
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { boardEvent } from '../../../web/e2e/helpers/whiteboard.ts';
import { uuid7 } from '../../../web/src/core/encoding/ids';
import { navTo } from '../fixtures/nav.ts';

const read = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));
const FOREIGN_SCENARIO = read('e2e-19-foreign-task');
const SCENARIO = read('e2e-19-platform-reads');
const WG_NAME = 'Research kz19-wg-r8t2y6u1';
const NAME = 'Ada kz19-name-c3v7b1n5';
const PROMPT = 'E2E19-READ kz19-prompt-e9w4q2z6: what can you see on the platform?';
const CANARIES = [
  'kz19-wg-r8t2y6u1',
  'kz19-name-c3v7b1n5',
  'kz19-prompt-e9w4q2z6',
  'kz19-prompt-f-k1m5x8',
  'kz19-answer-f2h6j9k3',
  'kz19-answer-s7d3g1h8',
  'kz19-title-n5b2v8c4',
  'kz19-board-o3p8w1q6',
  'kz19-board-w6e2r9t4',
];
const ORG_NOTE = 'Org note kz19-board-o3p8w1q6';
const WG_NOTE = 'Workgroup note kz19-board-w6e2r9t4';
/** Every read of the scenario, in call order. */
const READS = [
  'get_context',
  'list_workgroups',
  'list_members',
  'list_members',
  'list_tasks',
  'get_task',
  'get_task_messages',
  'get_whiteboard',
  'get_whiteboard',
  'get_whiteboard',
  'get_notifications',
  'list_operations',
  'get_operation',
  'get_task',
  'get_task_messages',
  'get_whiteboard',
];

interface JournalEntry {
  readonly path: string;
  readonly body: { readonly messages?: readonly { role: string; content: unknown }[] } | null;
}

interface ToolAnswer {
  readonly name: string;
  readonly input: Record<string, unknown>;
  readonly error: boolean;
  readonly value: unknown;
}

/** Every platform tool call of the session's last request with its decoded answer, in order. */
async function platformAnswers(orchestrator: Parameters<typeof hubOperations>[0]): Promise<ToolAnswer[]> {
  const journal = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as JournalEntry[];
  const requests = journal.filter((entry) => entry.path.startsWith('/v1/messages') && !entry.path.includes('count'));
  const messages = requests.at(-1)?.body?.messages ?? [];
  const calls = new Map<string, { name: string; input: Record<string, unknown> }>();
  const out: ToolAnswer[] = [];
  for (const message of messages) {
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
      out.push({ ...call, error: block.is_error === true, value: JSON.parse(text) as unknown });
    }
  }
  return out;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-19 @R-8.7 @R-8.8 @R-8.9 @R-P.5 @needs-vm the session reads what its owner may see, other readable workgroups included, and every read is logged', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-19 is a T1 test (07-testing.md section 2.1)');
  test.setTimeout(600_000);
  await orchestrator.loadScenario('model', FOREIGN_SCENARIO);
  await platformDevice(orchestrator);
  await poolReady(orchestrator, 2);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, NAME);
  const user = await PageUser.open(page, onboarded.recoveryCode);

  // A task in a second workgroup of the org: the reading session must not see into it.
  const research = await user.createWorkgroup(onboarded.org, WG_NAME);
  await home(page);
  await expect(page.getByTestId('home.wg').locator('option', { hasText: WG_NAME })).toHaveCount(1, { timeout: 30_000 });
  await page.getByTestId('home.wg').selectOption({ label: WG_NAME });
  const foreign = await sendPrompt(page, 'E2E19-FOREIGN kz19-prompt-f-k1m5x8: hello');
  await expectAnswerInChat(page, 'Foreign workgroup task kz19-answer-f2h6j9k3.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

  // The reading session in the default workgroup.
  await orchestrator.call('POST', '/doubles/model/scenario', {
    scenario: SCENARIO,
    variables: { foreign_task: foreign },
  });
  await home(page);
  const [org] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(onboarded.org)}'`,
  );
  // Whiteboard entries on the org and the default workgroup (written by the admin's client, E2E-25 covers the UI).
  const log = await user.log(onboarded.org);
  const boards = [
    { board: { org: onboarded.org, wg: null, task: null }, level: 'org', text: ORG_NOTE },
    { board: { org: onboarded.org, wg: org!.default_wg, task: null }, level: 'workgroup', text: WG_NOTE },
  ];
  for (const { board, level, text } of boards) {
    const event = await boardEvent(user, log, board, 'whiteboard.entry', uuid7(), { level, text });
    await user.request('POST', '/v1/events', { events: [{ event }] });
  }
  const options = page.getByTestId('home.wg').locator('option');
  await expect(options).toHaveCount(2, { timeout: 30_000 });
  const labels = (await options.allTextContents()).map((text) => text.trim());
  await page.getByTestId('home.wg').selectOption(org!.default_wg);
  const defaultLabel = (await page.getByTestId('home.wg').locator('option:checked').textContent())!.trim();
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'Platform read kz19-answer-s7d3g1h8.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);
  const [own] = await orchestrator.hubQuery<{ wg_id: string; owner_id: string }>(
    `SELECT wg_id, owner_id FROM hub.task WHERE id = '${uuid(task)}'`,
  );

  // The answers match the web state.
  const answers = await platformAnswers(orchestrator);
  expect(answers.map((answer) => answer.name)).toEqual([
    ...READS.slice(0, 11),
    'request_operation',
    ...READS.slice(11),
  ]);
  const value = (index: number) => answers[index]!.value as Record<string, unknown>;
  expect(answers.filter((answer) => answer.error)).toEqual([]);
  expect(value(0)).toMatchObject({
    workgroup: { id: own!.wg_id, name: defaultLabel, is_default: true },
    task: { id: task, owner: { id: own!.owner_id, display_name: NAME } },
    session: { id: session!.id, parent_session_id: null, depth: 0 },
    rights: { workgroup: 'write' },
    api_version: 1,
  });
  const workgroups = value(1).workgroups as {
    id: string;
    name: string;
    is_default: boolean;
    my_access: string | null;
  }[];
  expect(workgroups.map((wg) => wg.name).sort()).toEqual([...labels].sort());
  expect(workgroups.find((wg) => wg.name === WG_NAME)).toMatchObject({
    id: research,
    is_default: false,
    my_access: 'read',
  });
  expect(Object.keys(workgroups.find((wg) => wg.name === WG_NAME)!).sort()).toEqual([
    'id',
    'is_default',
    'my_access',
    'name',
  ]);
  for (const index of [2, 3])
    expect(value(index).members as { display_name: string }[]).toEqual(
      expect.arrayContaining([expect.objectContaining({ id: own!.owner_id, display_name: NAME })]),
    );
  const tasks = value(4).tasks as { id: string }[];
  expect(tasks.map((item) => item.id)).toContain(task);
  expect(tasks.map((item) => item.id)).not.toContain(foreign);
  expect(value(5)).toMatchObject({ id: task, owner_id: own!.owner_id });
  expect((value(5).sessions as { id: string }[]).map((item) => item.id)).toEqual([session!.id]);
  const messages = value(6).messages as { kind: string; text: string }[];
  expect(messages[0]).toMatchObject({ kind: 'prompt', text: PROMPT });
  const texts = (index: number) => value(index).entries as { text: string; author_id: string }[];
  expect(texts(7).map((entry) => [entry.text, entry.author_id])).toEqual([[ORG_NOTE, own!.owner_id]]);
  expect(texts(8).map((entry) => [entry.text, entry.author_id])).toEqual([[WG_NOTE, own!.owner_id]]);
  expect(value(9)).toEqual({ entries: [] });
  expect(value(10)).toEqual({ notifications: [] });
  const [operation] = await hubOperations(orchestrator, task);
  expect(operation).toMatchObject({ type: 'task.set_title', state: 'succeeded' });
  expect((value(12).operations as { operation_id: string }[]).map((item) => item.operation_id)).toEqual([
    operation!.id,
  ]);
  expect(value(13)).toMatchObject({ operation_id: operation!.id, status: 'succeeded' });
  await expect(
    page.getByTestId('operation.card').filter({ hasText: 'Platform reads kz19-title-n5b2v8c4' }),
  ).toBeVisible({
    timeout: 30_000,
  });

  // The other workgroup, readable for the owner (ADR-089): its task, its messages and its (empty) task whiteboard.
  expect(value(14)).toMatchObject({ id: foreign, owner_id: own!.owner_id });
  expect((value(15).messages as { kind: string; text: string }[])[0]).toMatchObject({
    kind: 'prompt',
    text: 'E2E19-FOREIGN kz19-prompt-f-k1m5x8: hello',
  });
  expect(value(16)).toEqual({ entries: [] });

  // Every read is audited and written as a platform.read event of the task (03-api.md section 8), get_notifications
  // too: the guest answers it from the session's own pings and journals the read (platform.read, KOR-07).
  const audit = await orchestrator.hubQuery<{
    action: string;
    result: string;
    session_id: string;
    detail: { read_wg_id?: string | null; triggered_by?: string | null };
  }>(
    `SELECT action, result, session_id, detail FROM hub.audit WHERE task_id = '${uuid(task)}' AND action LIKE 'read.session.%' ORDER BY seq`,
  );
  expect(audit.map((row) => row.action).sort()).toEqual(READS.map((tool) => `read.session.${tool}`).sort());
  expect(audit.every((row) => row.session_id === session!.id)).toBe(true);
  expect(audit.every((row) => row.result === 'ok')).toBe(true);
  // the last three reads are those of the other workgroup's task (ADR-089 section 4), on the owner's instruction
  const foreignReads = audit.slice(-3).map((row) => [row.action, row.detail.read_wg_id, row.detail.triggered_by]);
  expect(foreignReads.sort()).toEqual([
    ['read.session.get_task', research, own!.owner_id],
    ['read.session.get_task_messages', research, own!.owner_id],
    ['read.session.get_whiteboard', research, own!.owner_id],
  ]);
  const events = await taskBodies(page, orchestrator, onboarded, task, 'platform.read');
  expect(events.map((body) => body.tool).sort()).toEqual([...READS].sort());
  expect(audit.find((row) => row.action === 'read.session.get_notifications')?.result).toBe('ok');
  expect(events.find((body) => body.tool === 'get_notifications')).toEqual({
    tool: 'get_notifications',
    arguments: {},
    result_count: 0,
  });
  const tools = await taskBodies(page, orchestrator, onboarded, task, 'session.tool');
  expect(tools.filter((tool) => tool.name === 'mcp__platform__get_notifications')).toHaveLength(1);
  expect(events.find((body) => body.tool === 'list_members')).toMatchObject({ result_count: expect.any(Number) });
  expect(
    await orchestrator.hubQuery(
      `SELECT seq FROM hub.audit WHERE task_id = '${uuid(foreign)}' AND session_id = '${uuid(session!.id)}'`,
    ),
  ).toEqual([]);

  // The web shows the same whiteboard entries.
  await navTo(page, 'whiteboard');
  await expect(page.getByText(ORG_NOTE)).toBeVisible({ timeout: 30_000 });

  await expectNoPlaintext(orchestrator, CANARIES);
});
