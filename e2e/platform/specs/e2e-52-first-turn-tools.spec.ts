// E2E-52 (HOT-09; 05-runner.md section 6.1 step 6, 03-api.md section 8): the first turn of a new task works with the
// platform tools. A session from the warm pool gets the first prompt only after its agent listed the platform tools;
// the model double insists on them in the very first request (`tools_include`) and calls `get_context` and the
// operation `task.set_title` at once. Asserted: the double's script ran through (no mismatch), the first conversation
// request lists the platform tools, the operation exists at the hub, the answer reaches the chat, and the start stays
// under 10 s. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { hubOperations } from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  expectStartOrder,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  transitions,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-52-first-turn-tools.json', import.meta.url), 'utf8'),
);
const PROMPT = 'E2E48-FIRST kz48-prompt-t6w1: which workgroup is this?';
const PLATFORM_TOOLS = ['mcp__platform__get_context', 'mcp__platform__request_operation'];

interface JournalEntry {
  readonly path: string;
  readonly body: { readonly tools?: readonly { name: string }[]; readonly messages?: unknown[] } | null;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-52 @R-8.7 @R-P.5 @needs-vm the first turn of a new task on a pool VM uses a platform tool at once', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-52 is a T1 test');
  test.setTimeout(420_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator);

  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'Platform answered kz48-answer-v2c7.');
  // Every step of the script was taken in order: a first request without the platform tools would have been a
  // mismatch and ended the turn.
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);

  const journal = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as JournalEntry[];
  const conversation = journal.filter(
    (entry) =>
      entry.path.startsWith('/v1/messages') && !entry.path.includes('count') && (entry.body?.tools ?? []).length > 0,
  );
  const first = (conversation[0]?.body?.tools ?? []).map((tool) => tool.name);
  expect(first).toEqual(expect.arrayContaining(PLATFORM_TOOLS));

  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => op.type)).toEqual(['task.set_title']);

  const [session] = await mainSessions(orchestrator, task);
  expect(session!.seconds).not.toBeNull();
  expect(session!.seconds!).toBeLessThan(10);
  expectStartOrder(await transitions(orchestrator, session!.id));
});
