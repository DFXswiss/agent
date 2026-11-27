// E2E-73 (07-testing.md section 2.1; KOR-53, ADR-097; concept idea 33, section 8 points 5 and 6, rules E-05, E-19):
// "Optimize processes" starts a read-only analysis task over the usage, audit, operations and metrics its people may
// read; a person adopts the report.
// 1. Ada (admin of her organization) has the default workgroup and workgroup B with a task that renamed itself (one
//    operation), so B has usage, operations and audit rows; workgroup C belongs to another person's organization (an
//    admin reads every workgroup of her own organization, ADR-058, so the workgroup she has no right to lies outside
//    it). Many reads of the default workgroup's task list give it more audit rows than one page holds.
// 2. In the situation view she presses "Optimize processes", keeps the default workgroup and 7 days and starts: the
//    task opens with the badge, the profile `analysis` without a change button; the hub row has `analysis` (scope
//    `org`, seven days) and the profile `analysis`.
// 3. The session (brief in its system prompt, the four new tools offered, no web search) reads with get_usage,
//    list_workgroup_operations (group_by and rows), list_audit (page 1, then page 2 with `next_before`, then
//    group_by), get_metrics and list_workgroups; C is `not_found` (absent), B is answered. Its `task.set_title` is
//    refused `rights_denied` and audited. It answers a report with the six sections.
// 4. The chat shows the report; "Adopt" writes it to the workgroup whiteboard as Ada (the hub's entry and event name
//    Ada, a user, not a device).
// 5. The read log is complete (`read.session.<tool>` with `read_wg_id`, the refused read of C, the hub's
//    `read.device.workgroup.<resource>` rows, none for C); the hub holds no plaintext of names, prompts or the report.
// Runs on T1.
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { addAuthenticator } from '../fixtures/authenticator.ts';
import { auditRows, expectNoPlaintext } from '../fixtures/guest.ts';
import { hubOperations } from '../fixtures/github.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  home,
  invite,
  onboard,
  onboardWithCode,
  openChat,
  platformDevice,
  poolReady,
  sendPrompt,
  TASK_PATH,
  uuid,
} from '../fixtures/platform.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';

const scenario = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-73-process-analysis.json', import.meta.url), 'utf8'),
) as { variables: Record<string, string> } & Record<string, unknown>;
const ADA_NAME = 'Ada kz71-name-a6v2';
const BOB_NAME = 'Bob kz71-name-b9c4';
const WG_B = 'Beta kz71-wg-b-p8x3';
const B_PROMPT = 'E2E71-WGB kz71-prompt-b-f5j1: keep the budget of this workgroup';
const REPORT = 'Process analysis kz71-report-h5d2';
const SECTIONS = ['Efficiency', 'Token usage', 'Repetitions', 'Subagents', 'Workflows', 'Resources'];
const CANARIES = [
  'kz71-name-a6v2',
  'kz71-name-b9c4',
  'kz71-wg-b-p8x3',
  'kz71-prompt-b-f5j1',
  'kz71-title-b-r2t6',
  'kz71-answer-b-m3k8',
  'kz71-title-refused-w4n9',
  'kz71-report-h5d2',
];
/** Reads of the default workgroup's task list, each with another query: more audit rows than one page (50). */
const SEEDED_READS = 60;
const DAY_MS = 86_400_000;

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

/** The one answer of `name` whose input matches `input` (a subset). */
function answerOf(answers: readonly ToolAnswer[], name: string, input: Record<string, unknown>): ToolAnswer {
  const found = answers.filter(
    (answer) => answer.name === name && Object.entries(input).every(([key, value]) => answer.input[key] === value),
  );
  expect(
    found.map((answer) => answer.input),
    `${name} ${JSON.stringify(input)}`,
  ).toHaveLength(1);
  return found[0]!;
}

/** Sends `prompt` from the home composer in workgroup `wg`; returns the task. */
async function promptIn(page: Page, wg: string, prompt: string): Promise<string> {
  await home(page);
  const select = page.getByTestId('home.wg');
  await expect(select.locator(`option[value="${wg}"]`)).toHaveCount(1, { timeout: 30_000 });
  await select.selectOption(wg);
  return sendPrompt(page, prompt);
}

test('@E2E-73 @needs-vm process analysis reads only what its people may read, changes nothing and is adopted by a person', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-73 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(1_200_000);
  await platformDevice(orchestrator);
  await poolReady(orchestrator, 2);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, ADA_NAME);
  const org = onboarded.org;
  const ada = await PageUser.open(page, onboarded.recoveryCode);

  // 1. Workgroup C: the default workgroup of Bob's own organization (Ada has no right there).
  const bobContext = await browser.newContext({ baseURL: e2e.hubOrigin });
  const bobPage = await bobContext.newPage();
  await addAuthenticator(bobPage, 'platform');
  const bobOrg = (await onboardWithCode(bobPage, (await invite(orchestrator, 'user')).code, BOB_NAME)).org;
  await bobContext.close();
  const [wgC] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT id FROM hub.wg WHERE org_id = '${uuid(bobOrg)}' AND is_default`,
  );
  const [wgDefault] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT id FROM hub.wg WHERE org_id = '${uuid(org)}' AND is_default`,
  );
  const wg = wgDefault!.id;
  const wgc = wgC!.id;
  await orchestrator.loadScenario('model', { ...scenario, variables: { ...scenario.variables, wg_c: wgc } });

  // Workgroup B with a task that renames itself (one operation).
  const wgB = await ada.createWorkgroup(org, WG_B);
  const taskB = await promptIn(page, wgB, B_PROMPT);
  await expectAnswerInChat(page, 'Budget noted kz71-answer-b-m3k8.');
  expect((await hubOperations(orchestrator, taskB)).map((op) => [op.type, op.state])).toEqual([
    ['task.set_title', 'succeeded'],
  ]);
  // The audit volume of the default workgroup: reads with distinct queries are distinct audit rows.
  for (let limit = 1; limit <= SEEDED_READS; limit += 1)
    await ada.request('GET', `/v1/workgroups/${wg}/tasks?limit=${limit}`);
  const [seeded] = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.audit WHERE wg_id = '${uuid(wg)}'`,
  );
  expect(seeded!.n).toBeGreaterThan(SEEDED_READS);

  // 2. The button of the situation view, the default workgroup and 7 days.
  await home(page);
  await page.getByTestId('analysis.open').click();
  const dialog = page.getByTestId('analysis.dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId('analysis.wg')).toHaveValue(wg);
  await expect(dialog.getByTestId('analysis.period')).toHaveValue('7');
  await expect(dialog.getByTestId('analysis.visibility')).toHaveAttribute('data-scope', 'org');
  await dialog.getByTestId('analysis.start').click();
  await expect(page).toHaveURL(TASK_PATH, { timeout: 30_000 });
  const task = TASK_PATH.exec(page.url())![1]!;
  await expect(page.getByTestId('task.analysis')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('task.profile.name')).toHaveAttribute('data-profile', 'analysis');
  await expect(page.getByTestId('task.profile.analysis')).toBeVisible();
  await expect(page.getByTestId('task.profile.change')).toHaveCount(0);
  const [row] = await orchestrator.hubQuery<{
    wg_id: string;
    analysis: { scope: string; since: string; until: string };
    profile: string | null;
  }>(`SELECT wg_id, analysis, agent_profile->>'name' AS profile FROM hub.task WHERE id = '${uuid(task)}'`);
  expect([row!.wg_id, row!.analysis.scope, row!.profile]).toEqual([wg, 'org', 'analysis']);
  expect(Date.parse(row!.analysis.until) - Date.parse(row!.analysis.since)).toBe(7 * DAY_MS);

  // 3. and 4. The report in the chat (the second tab, KONZEPT E-43), with its six sections in order.
  await openChat(page);
  const output = page.getByTestId('chat.output').filter({ hasText: REPORT });
  await expect(output).toBeVisible({ timeout: 600_000 });
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  // the Markdown of a message is built once its row comes near the viewport (before that only its first line shows)
  await output.scrollIntoViewIfNeeded();
  await expect(output.getByRole('heading', { name: 'Resources' })).toBeVisible({ timeout: 30_000 });
  const shown = await output.innerText();
  const positions = SECTIONS.map((section) => shown.indexOf(section));
  expect(positions.every((position) => position >= 0)).toBe(true);
  expect([...positions].sort((a, b) => a - b)).toEqual(positions);

  // 3. What the tools answered.
  const answers = await platformAnswers(orchestrator, 'analysis-report');
  const { since, until } = row!.analysis;
  const period = { since, until };
  const listed = answerOf(answers, 'list_workgroups', {}).value.workgroups as { id: string; my_access: string }[];
  expect(listed.map((item) => item.id).sort()).toEqual([wg, wgB].sort());
  expect(listed.some((item) => item.id === wgc)).toBe(false);
  const own = answerOf(answers, 'get_usage', { ...period, workgroup_id: undefined });
  expect([own.error, own.value.workgroup_id, own.value.by]).toEqual([false, wg, 'task']);
  const ownGroups = answerOf(answers, 'list_workgroup_operations', { group_by: 'type', workgroup_id: undefined });
  expect([ownGroups.error, Array.isArray(ownGroups.value.groups)]).toEqual([false, true]);
  // B: answered, with its session, its operation and the counts.
  const usageB = answerOf(answers, 'get_usage', { workgroup_id: wgB, by: 'session' });
  expect([usageB.error, usageB.value.workgroup_id]).toEqual([false, wgB]);
  const sessionsB = usageB.value.rows as { task_id: string; depth: number; operations: number }[];
  expect(sessionsB.map((item) => [item.task_id, item.depth, item.operations])).toEqual([[taskB, 0, 1]]);
  expect(usageB.value.total).toMatchObject({ sessions: 1, tasks: 1, operations: 1, failed_operations: 0 });
  const groupsB = answerOf(answers, 'list_workgroup_operations', { workgroup_id: wgB, group_by: 'type' });
  expect(groupsB.value.groups).toEqual([{ type: 'task.set_title', state: 'succeeded', error_code: null, count: 1 }]);
  const operationsB = answerOf(answers, 'list_workgroup_operations', { workgroup_id: wgB, group_by: undefined });
  expect(
    (operationsB.value.operations as { task_id: string; type: string; state: string }[]).map((op) => [
      op.task_id,
      op.type,
      op.state,
    ]),
  ).toEqual([[taskB, 'task.set_title', 'succeeded']]);
  // C: absent - the same answer as a workgroup that does not exist.
  const usageC = answerOf(answers, 'get_usage', { workgroup_id: wgc });
  expect([usageC.error, usageC.value.error]).toEqual([true, 'not_found']);
  // The audit pages: page 1 is full and names the cursor, page 2 continues below it; the groups count the period.
  const page1 = answerOf(answers, 'list_audit', { before: undefined, group_by: undefined });
  const page2 = answers.find((answer) => answer.name === 'list_audit' && answer.input.before !== undefined)!;
  const rows1 = page1.value.rows as { seq: number }[];
  const rows2 = page2.value.rows as { seq: number }[];
  expect([page1.error, rows1.length, typeof page1.value.next_before]).toEqual([false, 50, 'string']);
  expect(page2.input.before).toBe(page1.value.next_before);
  expect([page2.error, rows2.length > 0]).toEqual([false, true]);
  expect(Math.max(...rows2.map((item) => item.seq))).toBeLessThan(Math.min(...rows1.map((item) => item.seq)));
  const auditGroups = answerOf(answers, 'list_audit', { group_by: 'action' }).value.groups as {
    action: string;
    count: number;
  }[];
  expect(auditGroups.find((group) => group.action === 'read.workgroup.tasks')?.count).toBeGreaterThanOrEqual(
    SEEDED_READS,
  );
  const metrics = answerOf(answers, 'get_metrics', { entity_kind: 'session' });
  expect([metrics.error, metrics.value.metric, Array.isArray(metrics.value.points)]).toEqual([
    false,
    'cpu_ratio',
    true,
  ]);
  // The refused write: rights_denied, registered and audited.
  const refused = answerOf(answers, 'request_operation', { type: 'task.set_title' });
  expect(refused.value).toMatchObject({ status: 'rejected', error: { code: 'rights_denied' } });
  expect((await hubOperations(orchestrator, task)).map((op) => [op.type, op.state, op.error_code])).toEqual([
    ['task.set_title', 'rejected', 'rights_denied'],
  ]);
  const rejections = (await auditRows(orchestrator, task, 'operation.rejected')).map((item) => item.detail.error_code);
  expect(rejections).toEqual(['rights_denied']);

  // 4. "Adopt": the report becomes an entry of the workgroup whiteboard, written by Ada.
  await output.getByTestId('analysis.adopt').click();
  const adopt = page.getByTestId('analysis.adopt.dialog');
  await expect(adopt.getByTestId('analysis.adopt.level.workgroup')).toBeChecked();
  await adopt.getByTestId('analysis.adopt.save').click();
  await expect(adopt.getByTestId('analysis.adopt.done')).toBeVisible({ timeout: 30_000 });
  await adopt.getByTestId('analysis.adopt.open').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: REPORT })).toBeVisible({ timeout: 30_000 });
  const entries = await orchestrator.hubQuery<{ level: string; wg_id: string; author_id: string; kind: string }>(
    `SELECT w.level, w.wg_id::text, w.author_id::text, e.author_kind AS kind FROM hub.whiteboard_entry w
       JOIN hub.event e ON e.id = w.created_event_id WHERE w.org_id = '${uuid(org)}'`,
  );
  expect(entries).toEqual([{ level: 'workgroup', wg_id: wg, author_id: ada.userId, kind: 'user' }]);

  // 5. The read log: every tool call names the workgroup read and Ada's instruction; the refused read of C too.
  const reads = (await auditRows(orchestrator, task)).filter((item) => item.action.startsWith('read.session.'));
  const detail = (item: (typeof reads)[number]) => item.detail as { read_wg_id?: string | null; triggered_by?: string };
  const count = (action: string) => reads.filter((item) => item.action === `read.session.${action}`).length;
  expect(count('get_usage')).toBeGreaterThanOrEqual(3);
  expect(count('list_audit')).toBeGreaterThanOrEqual(3);
  expect(count('list_workgroup_operations')).toBeGreaterThanOrEqual(3);
  expect(count('get_metrics')).toBeGreaterThanOrEqual(1);
  expect(count('list_workgroups')).toBeGreaterThanOrEqual(1);
  expect(reads.every((item) => detail(item).triggered_by === ada.userId)).toBe(true);
  const readOf = (action: string, wgId: string) =>
    reads.filter((item) => item.action === `read.session.${action}` && detail(item).read_wg_id === wgId);
  expect(readOf('get_usage', wgB).map((item) => item.result)).toEqual(['ok']);
  expect(readOf('get_usage', wg).map((item) => item.result)).toEqual(['ok']);
  const refusedRead = readOf('get_usage', wgc);
  expect(refusedRead).toHaveLength(1);
  expect(['denied', 'rejected']).toContain(refusedRead[0]!.result);
  // the hub's read log of the device routes: the analysed workgroups, never C
  const deviceReads = await orchestrator.hubQuery<{ action: string; wg_id: string; result: string }>(
    `SELECT action, wg_id::text, result FROM hub.audit WHERE action LIKE 'read.device.workgroup.%'
       AND wg_id IN ('${uuid(wg)}', '${uuid(wgB)}', '${uuid(wgc)}') ORDER BY seq`,
  );
  const deviceRead = (resource: string, wgId: string) =>
    deviceReads.filter((item) => item.action === `read.device.workgroup.${resource}` && item.wg_id === wgId);
  expect(deviceRead('usage', wg).length).toBeGreaterThanOrEqual(1);
  expect(deviceRead('usage', wgB).length).toBeGreaterThanOrEqual(1);
  expect(deviceRead('operations', wgB).length).toBeGreaterThanOrEqual(2);
  expect(deviceRead('audit', wg).length).toBeGreaterThanOrEqual(2);
  expect(deviceRead('metrics', wg).length).toBeGreaterThanOrEqual(1);
  expect(deviceReads.filter((item) => item.wg_id === wgc)).toEqual([]);
  expect(deviceReads.every((item) => item.result === 'ok')).toBe(true);

  await expectNoPlaintext(orchestrator, CANARIES);
});
