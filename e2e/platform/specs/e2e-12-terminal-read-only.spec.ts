// E2E-12 (07-testing.md section 2.1; 05-runner.md section 7; ADR-016): the read-only terminal view. While the session
// waits 12 s for the model, the view is open and receives the live frames: each new chunk reaches the browser less
// than a second after the recorder journaled it (p95 of `browser time − event ts`). Typing into the view sends
// nothing (no request leaves the page, the model never sees the text); text typed straight into the tmux pane (test
// hook) is refused by the prompt-origin hook and audited. After a reload the view replays the full history, and the
// decrypted `session.terminal` events equal the recorder's asciicast file byte for byte. Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { type Request } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, sessionCast, sessionClock, typeIntoPane } from '../fixtures/guest.ts';
import {
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-12-terminal-read-only.json', import.meta.url), 'utf8'),
);
const PROMPT = 'E2E12-TERMINAL kz12-prompt-b4n7d2s5: answer slowly';
const TYPED_IN_VIEW = 'kz12-view-typed-r1m5';
const TYPED_IN_PANE = 'kz12-pane-typed-g8h2 please run this';
const CANARIES = ['kz12-prompt-b4n7d2s5', 'kz12-answer-t6w3y8q1', 'kz12-name-p3v9c6x2', 'kz12-pane-typed-g8h2'];
const LIVE_LIMIT_MS = 1000;

type Frame = [number, string, string];

/** The asciicast output events of a cast file (header line dropped), times in ms. */
function castFrames(cast: string): Frame[] {
  return cast
    .split('\n')
    .slice(1)
    .filter((line) => line.length > 0)
    .map((line) => {
      const [seconds, code, data] = JSON.parse(line) as [number, string, string];
      return [Math.round(seconds * 1000), code, data];
    });
}

function percentile(values: readonly number[], p: number): number {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)]!;
}

test.describe.configure({ mode: 'serial' });

test('@E2E-12 @R-6.11 @R-6.12 @R-P.5 @needs-vm the terminal view is live, complete after a reload and read-only', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz12-name-p3v9c6x2');
  const task = await sendPrompt(page, PROMPT);

  // Open the session's terminal view through the app while the model is still "thinking".
  await page.getByTestId('task.tabs.sessions').click();
  await page.getByTestId('sessions.link').first().click();
  const terminal = page.getByTestId('terminal');
  await expect(terminal).toBeVisible({ timeout: 30_000 });
  await expect(terminal).toHaveAttribute('data-mode', 'follow');
  await expect(page.getByTestId('terminal.label')).toBeVisible();
  const [session] = await mainSessions(orchestrator, task);
  const sessionId = session!.id;
  await page.evaluate(() => {
    const element = document.querySelector('[data-testid="terminal"]')!;
    const seen: [number, number][] = [[Number(element.getAttribute('data-chunks')), Date.now()]];
    (window as unknown as { e2eChunks: [number, number][] }).e2eChunks = seen;
    new MutationObserver(() => seen.push([Number(element.getAttribute('data-chunks')), Date.now()])).observe(element, {
      attributes: true,
      attributeFilter: ['data-chunks'],
    });
  });
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 90_000 }).toBe(true);
  await expect
    .poll(
      async () =>
        (
          await orchestrator.hubQuery<{ n: number }>(
            `SELECT count(*)::int AS n FROM hub.event WHERE session_id = '${uuid(sessionId)}' AND kind = 'session.output'`,
          )
        )[0]!.n,
      { timeout: 60_000 },
    )
    .toBeGreaterThan(0);
  await page.waitForTimeout(2000);

  // Live: every chunk the hub received after the view opened (the hub and the browser share the hub device's
  // clock), from the event time the device wrote (recorder flush, VM clock corrected by the measured offset) to the
  // moment the open view applied it.
  const seen = await page.evaluate(() => (window as unknown as { e2eChunks: [number, number][] }).e2eChunks);
  const clock = await sessionClock(orchestrator, sessionId);
  const events = await orchestrator.hubQuery<{ ts_ms: number; received_ms: number }>(
    `SELECT (extract(epoch FROM ts) * 1000)::float8 AS ts_ms, (extract(epoch FROM received_at) * 1000)::float8 AS received_ms
       FROM hub.event WHERE session_id = '${uuid(sessionId)}' AND kind = 'session.terminal' ORDER BY scope_seq`,
  );
  const opened = seen[0]![1];
  const latencies: number[] = [];
  for (let index = 1; index < seen.length; index++) {
    const [count, at] = seen[index]!;
    if (count <= seen[index - 1]![0] || count > events.length) continue;
    const event = events[count - 1]!;
    if (event.received_ms >= opened) latencies.push(at - (event.ts_ms - clock.offset_ms));
  }
  test.info().annotations.push({
    type: 'terminal-live-ms',
    description:
      `n=${latencies.length} p50=${percentile(latencies, 50).toFixed(0)} p95=${percentile(latencies, 95).toFixed(0)}` +
      ` max=${Math.max(...latencies).toFixed(0)} vm-offset=${clock.offset_ms} rtt=${clock.rtt_ms}`,
  });
  expect(latencies.length).toBeGreaterThanOrEqual(5);
  expect(percentile(latencies, 95)).toBeLessThan(LIVE_LIMIT_MS);

  // No input path: typing into the view sends nothing and the model never sees it.
  const sent: Request[] = [];
  const listener = (request: Request) => {
    if (request.method() !== 'GET') sent.push(request);
  };
  page.on('request', listener);
  const sockets: string[] = [];
  page.on('websocket', (socket) => sockets.push(socket.url()));
  const front = page.locator('[data-testid^="terminal.screen."]:not(.invisible)');
  const charsBefore = await front.getAttribute('data-chars');
  await page.getByTestId('terminal.viewport').click();
  await page.keyboard.type(TYPED_IN_VIEW);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2000);
  page.off('request', listener);
  expect(sent.map((request) => `${request.method()} ${request.url()}`)).toEqual([]);
  expect(sockets).toEqual([]);
  expect(await front.getAttribute('data-chars')).toBe(charsBefore);

  // Text typed straight into the pane is refused by the prompt-origin hook and audited; the model sees nothing.
  await typeIntoPane(orchestrator, sessionId, TYPED_IN_PANE);
  await expect
    .poll(async () => (await auditRows(orchestrator, task, 'session.prompt_refused')).length, { timeout: 30_000 })
    .toBe(1);
  const [refused] = await auditRows(orchestrator, task, 'session.prompt_refused');
  expect(refused).toMatchObject({ session_id: sessionId });
  await page.waitForTimeout(2000);
  const state = await orchestrator.doubleState('model');
  expect(state.ok).toBe(true);
  expect(state.failures).toEqual([]);

  // History after a reload (the passkey unlocks again): the view replays everything that was recorded.
  await expect
    .poll(async () => castFrames(await sessionCast(orchestrator, sessionId)).length, { timeout: 30_000 })
    .toBeGreaterThan(0);
  await page.waitForTimeout(1500); // the last recorder flush (100 ms) reaches the hub
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/s/${sessionId}$`), { timeout: 30_000 });
  // The decrypted events equal the recorder's file (polled: the newest flush may still be on its way).
  let bodies: Record<string, unknown>[] = [];
  let history: Frame[] = [];
  await expect
    .poll(
      async () => {
        bodies = await taskBodies(page, orchestrator, onboarded, task, 'session.terminal');
        history = bodies.flatMap((body) => body.frames as Frame[]);
        const cast = castFrames(await sessionCast(orchestrator, sessionId));
        return history.length > 0 && JSON.stringify(history) === JSON.stringify(cast);
      },
      { timeout: 30_000, intervals: [1000] },
    )
    .toBe(true);
  expect(history.map((frame) => frame[2]).join('')).toContain('kz12-answer-t6w3y8q1');
  expect(history.map((frame) => frame[2]).join('')).not.toContain(TYPED_IN_VIEW);
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-chunks', String(bodies.length), { timeout: 30_000 });
  const output = history.filter((frame) => frame[1] === 'o').map((frame) => frame[2]);
  await expect(page.locator('[data-testid^="terminal.screen."]:not(.invisible)')).toHaveAttribute(
    'data-chars',
    String(output.join('').length),
  );

  await expectNoPlaintext(orchestrator, CANARIES);
});
