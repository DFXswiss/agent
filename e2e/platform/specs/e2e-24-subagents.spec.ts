// E2E-24 (07-testing.md section 2.1; 03-api.md sections 8.1, 8.3, 9, 12; 05-runner.md section 13; ADR-017): controlled
// subagents. The session requests a reviewer through the platform operation `subagent.request` and waits for it with
// `get_subagent_result`; the child requests a grandchild the same way; the grandchild's own request exceeds the depth
// limit. Each subagent is a full session of the same task in its own sandbox (own session user) of the workgroup's VM,
// linked to its parent and to the operation that started it, with its terminal in the web. Four children requested at
// once: three run, the fourth exceeds the limit of running children per task. Claude Code's own Agent/Task tools are
// never offered nor used. Runs on T1 and T2.
// Pending (SCN-07): "child session appears in the monitoring overview with its own samples" - checked by SCN-08/ACC-M2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore, vmFiles } from '../fixtures/guest.ts';
import { hubOperations } from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-24-subagents.json', import.meta.url), 'utf8'),
);
const PROMPT = 'E2E24-SUB kz24-prompt-h2j6k9l3: get the notes reviewed by subagents';
const CANARIES = [
  'kz24-prompt-h2j6k9l3',
  'kz24-purpose-c',
  'kz24-purpose-g',
  'kz24-grandchild-w5e8r2',
  'kz24-child-t9y3u6',
  'kz24-par-a7s1d4',
  'kz24-answer-o4i8p1',
  'kz24-name-b6n1m4q7',
];

interface SessionTree {
  readonly id: string;
  readonly parent_session_id: string | null;
  readonly origin_operation_id: string | null;
  readonly depth: number;
  readonly state: string;
  readonly device_id: string;
  readonly vm_id: string | null;
}

interface JournalEntry {
  readonly path: string;
  readonly body: {
    readonly tools?: readonly { name: string }[];
    readonly messages?: readonly { role: string; content: unknown }[];
  } | null;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-24 @R-10.6 @R-13.1 @R-13.2 @R-13.3 @R-13.4 @R-13.5 @R-13.6 @R-13.7 @R-13.8 @R-P.5 @needs-vm subagents are requested through the platform, run as linked sessions in their own sandboxes and stay within the limits', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(720_000);
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz24-name-b6n1m4q7');
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'Subagents finished kz24-answer-o4i8p1.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [main] = await mainSessions(orchestrator, task);

  // The operations: child and grandchild started, the great-grandchild and the fourth parallel child refused.
  const operations = (await hubOperations(orchestrator, task)).filter((op) => op.type === 'subagent.request');
  expect(operations.map((op) => [op.state, op.error_code])).toEqual([
    ['succeeded', null],
    ['succeeded', null],
    ['rejected', 'limit_exceeded'],
    ['succeeded', null],
    ['succeeded', null],
    ['succeeded', null],
    ['rejected', 'limit_exceeded'],
  ]);

  // The session tree: same task, linked to the requesting session and the starting operation, depth counted.
  let sessions: SessionTree[] = [];
  await expect
    .poll(
      async () =>
        (sessions = await orchestrator.hubQuery<SessionTree>(
          `SELECT id, parent_session_id, origin_operation_id, depth, state, device_id, vm_id FROM hub.session
            WHERE task_id = '${uuid(task)}' ORDER BY created_at`,
        )).filter((row) => row.depth > 0 && row.state === 'stopped').length,
      { timeout: 120_000 },
    )
    .toBe(5);
  const [parent, child, grandchild, ...parallel] = sessions;
  expect(parent!.id).toBe(main!.id);
  expect([child, grandchild, ...parallel].map((row) => [row!.parent_session_id, row!.depth])).toEqual([
    [parent!.id, 1],
    [child!.id, 2],
    [parent!.id, 1],
    [parent!.id, 1],
    [parent!.id, 1],
  ]);
  const started = operations.filter((op) => op.state === 'succeeded').map((op) => op.id);
  expect([child, grandchild, ...parallel].map((row) => row!.origin_operation_id)).toEqual(started);
  expect(operations[0]!.session_id).toBe(parent!.id);
  expect(operations[1]!.session_id).toBe(child!.id);
  expect(operations[2]!.session_id).toBe(grandchild!.id);
  expect(new Set(sessions.map((row) => row.device_id))).toEqual(new Set([device]));

  // Own sandboxes: all in the workgroup's VM, each with its own workspace owned by its own session user.
  const local = await runnerStore<{ id: string; vm_id: string }>(
    orchestrator,
    `SELECT id::text, vm_id FROM runner.session WHERE task_id = '${uuid(task)}'::uuid`,
  );
  expect(local).toHaveLength(6);
  expect(new Set(local.map((row) => row.vm_id)).size).toBe(1);
  const workspaces = sessions.map((row) => `/srv/sessions/${row.id}/work`);
  const facts = Object.values(await vmFiles(orchestrator, workspaces, parent!.id))[0]!;
  const owners = workspaces.map((path) => facts[path]?.owner);
  expect(owners.every((owner) => owner !== undefined && owner !== 'root')).toBe(true);
  expect(new Set(owners).size).toBe(6);

  // get_subagent_result gave the parent the stopped child's final text; nobody used Claude Code's own subagent tools.
  const journal = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as JournalEntry[];
  const requests = journal.filter((entry) => entry.path.startsWith('/v1/messages') && !entry.path.includes('count'));
  const offered = new Set(requests.flatMap((entry) => (entry.body?.tools ?? []).map((tool) => tool.name)));
  expect(offered.has('Agent') || offered.has('Task')).toBe(false);
  const results = requests
    .flatMap((entry) => entry.body?.messages ?? [])
    .flatMap((message) => (Array.isArray(message.content) ? (message.content as Record<string, unknown>[]) : []))
    .filter((block) => block.type === 'tool_result')
    .map((block) => JSON.stringify(block.content));
  expect(
    results.some((text) => text.includes(child!.id) && text.includes('stopped') && text.includes('kz24-child-t9y3u6')),
  ).toBe(true);
  const tools = await taskBodies(page, orchestrator, onboarded, task, 'session.tool');
  expect(tools.length).toBeGreaterThan(0);
  expect(tools.filter((tool) => tool.name === 'Agent' || tool.name === 'Task')).toEqual([]);

  // The web: the session tree of the task, the child's terminal and its own child with the requested purpose.
  await page.getByTestId('task.tabs.sessions').click();
  await expect(page.getByTestId('sessions.node')).toHaveCount(6, { timeout: 30_000 });
  await expect(page.locator(`[data-testid="sessions.node"][data-session="${grandchild!.id}"]`)).toHaveAttribute(
    'data-depth',
    '2',
  );
  await page.locator(`[data-testid="sessions.node"][data-session="${child!.id}"] > a`).click();
  await expect(page).toHaveURL(new RegExp(`/s/${child!.id}$`));
  await expect(page.getByTestId('terminal')).toBeVisible({ timeout: 30_000 });
  await expect
    .poll(async () => Number(await page.getByTestId('terminal').getAttribute('data-chunks')), { timeout: 30_000 })
    .toBeGreaterThan(0);
  await page.getByTestId('session.tabs.children').click();
  const grand = page.locator(`[data-testid="session.child"][data-session="${grandchild!.id}"]`);
  await expect(grand).toBeVisible({ timeout: 30_000 });
  await expect(grand.getByTestId('children.purpose')).toHaveText('Purpose: Check the wording kz24-purpose-g');

  await expectNoPlaintext(orchestrator, CANARIES);
});
