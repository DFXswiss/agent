// Shared steps of PERF-03 and PERF-04 (07-testing.md section 2.2; 03-api.md section 5; 05-runner.md section 7): one
// user with one session whose terminal view is open (the browser watches the session's terminal, so every recorder
// flush becomes a `terminal` frame), terminal output written by the orchestrator into the session's pane
// (`POST /latency/marks`, one line every `MARK_INTERVAL_MS`, more than the recorder's 100 ms flush, so nearly every line
// is an event of its own), and the browser-side probes: the arrival time of every live-stream frame (a tap on the
// page's `fetch` of `/v1/stream`, added before the app loads; the app reads its own branch of the stream unchanged) and
// the render time of every terminal chunk (the view's `data-chunks` count, two animation frames later: xterm renders
// in the next frame after the write). The orchestrator matches the samples with the device-side times and writes the
// CSV rows (`orchestrator/routes/latency.py`).
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
} from '../fixtures/platform.ts';

/** Measured events of PERF-03 (07-testing.md: 1000); `E2E_PERF_EVENTS` overrides it. */
export const EVENTS = Number(process.env.E2E_PERF_EVENTS ?? '1000');
/** Measured terminal chunks of PERF-04; `E2E_PERF_TERMINAL_EVENTS` overrides it. */
export const TERMINAL_EVENTS = Number(process.env.E2E_PERF_TERMINAL_EVENTS ?? '300');
/** Spacing of the marker lines (above the recorder's 100 ms flush interval). */
export const MARK_INTERVAL_MS = Number(process.env.E2E_PERF_MARK_INTERVAL_MS ?? '150');
/** Lines written per measured event: a few flushes may carry two lines or be swallowed by a busy device. */
export const MARK_FACTOR = 1.15;

const PROMPT = 'PERF-LATENCY kzperf-prompt: say ready';
export const ANSWER = 'Latency probe ready kzperf-answer.';

export const scenario = {
  name: 'perf-latency',
  description:
    'PERF-03/PERF-04: one short answer; the measured load is terminal output the orchestrator writes into the pane.',
  steps: [
    {
      id: 'ready',
      expect: { user_text_contains: 'PERF-LATENCY', has_tools: true, stream: true },
      reply: { content: [{ type: 'text', text: ANSWER }] },
    },
  ],
  side: [{ id: 'title', expect: { has_tools: false }, reply: { content: [{ type: 'text', text: 'Latency' }] } }],
};

export type Frame = [scopeId: string, scopeSeq: number, atMs: number];
export type Render = [chunks: number, atMs: number];

export interface Summary {
  readonly samples: number;
  readonly p50_ms: number | null;
  readonly p95_ms: number | null;
  readonly max_ms: number | null;
  readonly limit_ms: number;
  readonly passed: boolean;
}

export interface Evaluation {
  readonly summary: Summary;
  readonly clock: { readonly offset_ms: number; readonly rtt_ms: number };
  readonly load: Readonly<Record<string, string>>;
  readonly unmatched: Readonly<Record<string, number>>;
  readonly csv: string;
}

interface Probes {
  e2eFrames: Frame[];
  e2eRenders: Render[];
}

/**
 * Records the arrival of every `event`/`terminal` frame of the page's live stream as `[scope_id, scope_seq, Date.now()]`
 * (first arrival per frame). Must run before the app loads (an init script of the page).
 */
export async function tapStream(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const probes = window as unknown as Probes;
    probes.e2eFrames = [];
    const seen = new Set<string>();
    const original = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await original(input, init);
      const url = input instanceof Request ? input.url : String(input);
      const type = response.headers.get('content-type') ?? '';
      if (!url.includes('/v1/stream') || !response.ok || !type.includes('text/event-stream') || response.body === null)
        return response;
      const [app, tap] = response.body.tee();
      Object.defineProperty(response, 'body', { value: app });
      void (async () => {
        const reader = tap.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { done, value } = await reader.read().catch(() => ({ done: true, value: undefined }));
          if (done || value === undefined) return;
          const at = Date.now();
          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop()!;
          for (const block of blocks) {
            const lines = block.split(/\r?\n/);
            const event = lines
              .find((line) => line.startsWith('event:'))
              ?.slice(6)
              .trim();
            if (event !== 'event' && event !== 'terminal') continue;
            const data = lines
              .filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trimStart())
              .join('\n');
            const frame = JSON.parse(data) as { scope_id: string; scope_seq: number };
            const key = `${frame.scope_id}:${frame.scope_seq}`;
            if (seen.has(key)) continue;
            seen.add(key);
            probes.e2eFrames.push([frame.scope_id, frame.scope_seq, at]);
          }
        }
      })();
      return response;
    };
  });
}

/** The frames recorded by `tapStream` that arrived at or after `since`. */
export async function framesSince(page: Page, since: number): Promise<Frame[]> {
  const frames = await page.evaluate(() => (window as unknown as Probes).e2eFrames);
  return frames.filter((frame) => frame[2] >= since);
}

/**
 * A new user (platform invitation, passkey) in a new private org sends one prompt; once the answer is in the chat the
 * session's terminal view is opened through the app (sessions tab) and follows the live output. Returns the session.
 */
export async function openTerminal(page: Page, orchestrator: Orchestrator, name: string): Promise<string> {
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await onboard(page, orchestrator, name);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, ANSWER);
  const [session] = await mainSessions(orchestrator, task);
  await page.getByTestId('task.tabs.sessions').click();
  await page.getByTestId('sessions.link').first().click();
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-mode', 'follow', { timeout: 30_000 });
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  return session!.id;
}

/**
 * Starts recording `[data-chunks, render time]` of the terminal view: on every change of the applied chunk count, the
 * time two animation frames later (the first sample is the state now). Returns the start time.
 */
export async function watchRenders(page: Page): Promise<number> {
  return page.evaluate(() => {
    const element = document.querySelector('[data-testid="terminal"]')!;
    const probes = window as unknown as Probes;
    const count = () => Number(element.getAttribute('data-chunks'));
    const opened = Date.now();
    probes.e2eRenders = [[count(), opened]];
    new MutationObserver(() => {
      const chunks = count();
      requestAnimationFrame(() => requestAnimationFrame(() => probes.e2eRenders.push([chunks, Date.now()])));
    }).observe(element, { attributes: true, attributeFilter: ['data-chunks'] });
    return opened;
  });
}

export async function renders(page: Page): Promise<Render[]> {
  return page.evaluate(() => (window as unknown as Probes).e2eRenders);
}

/** Starts the marker lines in the session's pane; resolves with the time the writer needs. */
export async function startMarks(orchestrator: Orchestrator, session: string, events: number): Promise<number> {
  const count = Math.ceil(events * MARK_FACTOR);
  await orchestrator.call('POST', '/latency/marks', { session, count, interval_ms: MARK_INTERVAL_MS });
  return count * MARK_INTERVAL_MS;
}

/** The hub's number of `session.terminal` events of `session`. */
export async function terminalEvents(orchestrator: Orchestrator, session: string): Promise<number> {
  const [row] = await orchestrator.hubQuery<{ n: number }>(
    `SELECT count(*)::int AS n FROM hub.event WHERE session_id = '${session}' AND kind = 'session.terminal'`,
  );
  return row!.n;
}

/** Logs the evaluation and attaches it to the test's annotations (the report quotes it). */
export function report(label: string, evaluation: Evaluation): void {
  const text = JSON.stringify(evaluation);
  console.log(`${label}: ${text}`);
  test.info().annotations.push({ type: label, description: text });
}
