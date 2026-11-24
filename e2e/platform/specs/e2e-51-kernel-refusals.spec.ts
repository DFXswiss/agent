// E2E-51 (07-testing.md section 2.1; KOR-60, decision D16 of the concept review; 08-monitoring.md section 3): a
// blocked attempt that reaches the kernel is counted per session in the monitoring. The session reads a file inside
// its own workspace - the PreToolUse policy allows that - which the spec has put there owned by root with mode 0600
// while the session waited, so the kernel refuses the read (EACCES): the hook let a call through that the second
// layer stopped. The guest's session metrics (the source of `agent_runner_session_sandbox_denials_total`) count it;
// no hook denied anything. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { sessionVm } from '../fixtures/guest.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-51-kernel-refusals.json', import.meta.url), 'utf8'),
);
const LOCKED = 'kz49-locked-notes.txt';

interface GuestSession {
  readonly sandbox_denials?: number;
  readonly hook_denies?: number;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-51 @R-10.12 @needs-vm a read the hook allows and the kernel refuses is counted for the session', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-51 reads the guest of the runner device (T1)');
  test.setTimeout(300_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada kz49-name-r6t2');
  const task = await sendPrompt(page, 'E2E49-LOCKED summarise the locked notes');

  // While the double holds the session's first answer back: the root-owned file appears in the workspace.
  let session = '';
  await expect
    .poll(async () => (session = (await mainSessions(orchestrator, task))[0]?.id ?? ''), { timeout: 60_000 })
    .not.toBe('');
  await expect
    .poll(
      async () => {
        try {
          return (await sessionVm(orchestrator, session)).unit;
        } catch {
          return 'unknown';
        }
      },
      { timeout: 60_000, intervals: [500] },
    )
    .toBe('active');
  const planted = await orchestrator.call<{ path: string }>('POST', `/sessions/${uuid(session)}/locked-file`, {
    name: LOCKED,
    text: 'kz49-secret-q1w7\n',
  });
  expect(planted.path).toBe(`/srv/sessions/${uuid(session)}/work/${LOCKED}`);

  await expectAnswerInChat(page, 'The kernel refused the read kz49-answer-v3m8.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const metrics = async () =>
    (await orchestrator.call<{ session: GuestSession | null }>('GET', `/sessions/${uuid(session)}/guest-metrics`))
      .session;
  await expect.poll(async () => (await metrics())?.sandbox_denials ?? 0, { timeout: 60_000 }).toBe(1);
  expect((await metrics())?.hook_denies).toBe(0);
});
