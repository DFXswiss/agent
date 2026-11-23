// E2E-13 (07-testing.md section 2.1; 05-runner.md sections 6.2-6.4): chat and follow-ups. A prompt makes the session
// write and read a file in its workspace and answer; the chat shows the prompt, the tool summaries, the file change
// and the answers in order. With a short idle interval the idle session is suspended (unit stopped, transcript kept);
// a follow-up from the chat resumes it with `--resume`: the model double sees the earlier turn in the request and the
// hub the transitions `suspended` → `resuming` → `working`. The hub side holds none of the texts (plaintext scan).
// Runs on T1 and T2.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, sessionState, sessionVm, setIdle } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  transitions,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-13-chat-follow-ups.json', import.meta.url), 'utf8'),
);
const PROMPT = 'E2E13-NOTES kz13-prompt-x5d1n8b3: write notes.md and read it back';
const FOLLOW_UP = 'E2E13-FOLLOW kz13-follow-c7j3e5a2: what does notes.md hold now?';
const NAME = 'Ada kz13-name-h2p9s4v6';
const FILE = '# Notes\nkz13-file-q8v2r6t1\n';
const OUTPUTS = [
  'I will write the notes file first.',
  'The notes file holds kz13-answer-m4k7w2c9.',
  'Resumed: notes.md still holds kz13-file-q8v2r6t1.',
];
const CANARIES = [
  'kz13-prompt-x5d1n8b3',
  'kz13-follow-c7j3e5a2',
  'kz13-name-h2p9s4v6',
  'kz13-file-q8v2r6t1',
  'kz13-answer-m4k7w2c9',
  'E2E13-NOTES',
];
const IDLE_SECONDS = 60;

test.describe.configure({ mode: 'serial' });

test('@E2E-13 @R-6.3 @R-6.10 @R-P.5 @needs-vm the chat shows prompts, tools, file changes and answers; a follow-up resumes the suspended session', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  await setIdle(orchestrator, IDLE_SECONDS);
  try {
    await orchestrator.loadScenario('model', scenario);
    await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, NAME);
    const task = await sendPrompt(page, PROMPT);

    // First turn: prompt, both tool summaries, the file change and the answers, in this order.
    await expectAnswerInChat(page, OUTPUTS[1]!);
    await expect(page.getByTestId('chat.prompt.text')).toHaveText([PROMPT]);
    await expect(page.getByTestId('chat.tool.summary')).toHaveText([/^Write .*notes\.md/, /^Read .*notes\.md/]);
    await expect(page.getByTestId('chat.file.path')).toContainText(['notes.md']);
    await expect(page.getByTestId('chat.output')).toHaveText([/first\.$/, /m4k7w2c9\.$/]);
    const kinds = await page.getByTestId('chat.item').evaluateAll((items) => items.map((item) => item.dataset.kind));
    const at = (kind: string) => kinds.indexOf(kind);
    expect(at('prompt'), kinds.join(',')).toBeGreaterThanOrEqual(0);
    expect(at('prompt')).toBeLessThan(at('session.tool'));
    expect(at('session.tool')).toBeLessThan(at('session.file'));
    expect(at('session.file')).toBeLessThan(kinds.lastIndexOf('session.output'));
    const files = await taskBodies(page, orchestrator, onboarded, task, 'session.file');
    expect(files).toEqual([
      expect.objectContaining({
        path: 'notes.md',
        op: 'write',
        size: FILE.length,
        sha256: createHash('sha256').update(FILE).digest('hex'),
      }),
    ]);
    const tools = await taskBodies(page, orchestrator, onboarded, task, 'session.tool');
    expect(tools.map((tool) => [tool.name, tool.is_error])).toEqual([
      ['Write', false],
      ['Read', false],
    ]);

    // Idle → suspended after the short idle interval (unit stopped, transcript kept).
    const [session] = await mainSessions(orchestrator, task);
    await expect
      .poll(() => sessionState(orchestrator, session!.id), { timeout: (IDLE_SECONDS + 120) * 1000, intervals: [2000] })
      .toBe('suspended');

    // The follow-up from the chat resumes the same session; the model sees the whole earlier turn (min_messages).
    await page.getByTestId('chat.composer.prompt').fill(FOLLOW_UP);
    await page.getByTestId('chat.composer.send').click();
    await expectAnswerInChat(page, OUTPUTS[2]!);
    await expect(page.getByTestId('chat.prompt.text')).toHaveText([PROMPT, FOLLOW_UP]);
    await expect(page.getByTestId('chat.output')).toHaveText([/first\.$/, /m4k7w2c9\.$/, /q8v2r6t1\.$/]);
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
    expect(await mainSessions(orchestrator, task)).toHaveLength(1);
    const states = await transitions(orchestrator, session!.id);
    const suspended = states.indexOf('suspended');
    expect(suspended, states.join(',')).toBeGreaterThan(states.indexOf('working'));
    expect(states.slice(suspended)).toEqual(expect.arrayContaining(['suspended', 'resuming', 'working']));
    expect(states.indexOf('resuming', suspended)).toBeLessThan(states.indexOf('working', suspended));
    // The resumed unit runs Claude Code with `--resume <claude session id>`.
    const command = (await sessionVm(orchestrator, session!.id)).command;
    expect(command.indexOf('--resume'), command.join(' ')).toBeGreaterThan(0);
    expect(command[command.indexOf('--resume') + 1]).toMatch(/^[0-9a-f-]{36}$/);
    expect((await taskBodies(page, orchestrator, onboarded, task, 'prompt')).map((body) => body.text)).toEqual([
      PROMPT,
      FOLLOW_UP,
    ]);
    expect((await taskBodies(page, orchestrator, onboarded, task, 'session.output')).map((body) => body.text)).toEqual(
      OUTPUTS,
    );

    await expectNoPlaintext(orchestrator, CANARIES);
  } finally {
    await setIdle(orchestrator, null);
  }
});
