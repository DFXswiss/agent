// E2E-23 (07-testing.md section 2.1; 05-runner.md sections 4.1, 5.3, 8; KONZEPT section 10): execution ban and harness.
// An adversary turn asks for shell, git, tests and subagents: Claude Code is offered none of these tools (the first
// model request lists only Read/Glob/Grep/Edit/Write and the platform tools) and refuses the calls; the file tools
// pointed outside the workspace are denied by the PreToolUse policy, journaled as `session.hook` (`deny`) and audited
// as `hook.denied` (tool and rule, never the path). The first request carries the harness: the rules, the context with
// org, workgroup, task and session, and the platform API. Nothing was executed: no file appeared in the workspace.
// Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, vmFiles } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-23-execution-ban.json', import.meta.url), 'utf8'),
);
const PROMPT = 'E2E23-EXEC kz23-prompt-u2r7f5h3: run git status, the tests and a build, use a subagent';
const CANARIES = ['kz23-prompt-u2r7f5h3', 'kz23-answer-v8e4q1w6', 'kz23-name-o6t1b9d4'];

interface JournalEntry {
  readonly path: string;
  readonly body: { readonly system?: unknown; readonly tools?: readonly { name: string }[] } | null;
}

/** The system prompt text of a Messages request (string or text blocks). */
function systemText(body: JournalEntry['body']): string {
  const system = body?.system;
  if (typeof system === 'string') return system;
  return Array.isArray(system) ? system.map((block: { text?: string }) => block.text ?? '').join('\n') : '';
}

test.describe.configure({ mode: 'serial' });

test('@E2E-23 @R-9.7 @R-10.1 @R-10.2 @R-10.3 @R-10.4 @R-10.7 @R-10.12 @R-P.5 @needs-vm shell, git, tests and subagents are absent or denied; the harness names the context and the platform API', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(420_000);
  await orchestrator.loadScenario('model', scenario);
  await orchestrator.resetDouble('model');
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz23-name-o6t1b9d4');
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'Execution is not possible here kz23-answer-v8e4q1w6.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);
  const sessionId = session!.id;

  // Every tool call failed; the forbidden tools were never offered, so they never ran.
  const tools = await taskBodies(page, orchestrator, onboarded, task, 'session.tool');
  expect(tools.map((tool) => [tool.name, tool.is_error])).toEqual([
    ['Bash', true],
    ['Agent', true],
    ['Task', true],
    ['Glob', true],
    ['Read', true],
    ['Write', true],
  ]);
  const requests = (
    (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as JournalEntry[]
  ).filter(
    (entry) =>
      entry.path.startsWith('/v1/messages') &&
      !entry.path.includes('count_tokens') &&
      (entry.body?.tools ?? []).length > 0,
  );
  expect(requests.length).toBeGreaterThan(0);
  const offered = new Set(requests.flatMap((entry) => (entry.body?.tools ?? []).map((tool) => tool.name)));
  for (const name of ['Bash', 'Agent', 'Task', 'WebFetch', 'WebSearch', 'NotebookEdit', 'Skill'])
    expect(offered).not.toContain(name);

  // The harness of the first request: rules, the session's context and the platform API.
  const [row] = await orchestrator.hubQuery<{ org_id: string; wg_id: string }>(
    `SELECT org_id, wg_id FROM hub.task WHERE id = '${task}'`,
  );
  const system = systemText(requests[0]!.body);
  for (const fact of [
    'agent.dfx.swiss session rules',
    'You must not start subagents yourself.',
    'mcp__platform__request_operation',
    '# Context of this session',
    `(\`${row!.org_id}\`)`,
    `(\`${row!.wg_id}\`)`,
    `- Task ID: \`${task}\``,
    `- Session ID: \`${sessionId}\``,
    '## Platform tools',
  ])
    expect(system).toContain(fact);

  // Blocked attempts are journaled and audited (tool and rule only).
  const hooks = await taskBodies(page, orchestrator, onboarded, task, 'session.hook');
  const denied = hooks.filter((body) => body.event === 'PreToolUse' && body.decision === 'deny');
  expect(denied.map((body) => [body.tool, body.rule])).toEqual(
    expect.arrayContaining([
      ['Glob', 3],
      ['Read', 6],
    ]),
  );
  const audit = await auditRows(orchestrator, task, 'hook.denied');
  expect(audit.map((item) => [item.session_id, item.detail.tool, item.detail.rule])).toEqual(
    expect.arrayContaining([
      [sessionId, 'Glob', 3],
      [sessionId, 'Read', 6],
    ]),
  );
  expect(audit.length).toBe(denied.length);
  for (const item of audit) expect(JSON.stringify(item.detail)).not.toContain('/usr/bin');

  // Nothing was executed or written: the workspace holds no git hook.
  const files = await vmFiles(orchestrator, [`/srv/sessions/${sessionId}/work/.git`], sessionId);
  expect(Object.values(files)[0]).toEqual({ [`/srv/sessions/${sessionId}/work/.git`]: null });

  await expectNoPlaintext(orchestrator, CANARIES);
});
