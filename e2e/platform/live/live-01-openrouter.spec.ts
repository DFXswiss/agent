// LIVE-01 (07-testing.md sections 2.4 and 6; LVT-01, built on the SCN-05 smoke): the platform's OpenRouter access with
// DeepSeek over the whole chain against the real provider. The operator's platform OpenRouter key arrives only in the
// run's environment (`E2E_LIVE_OPENROUTER_KEY`, handed over by `live/driver.py` on standard input; every artefact is
// scanned for it). The runners' upstreams point at the live bridge on the runner device (`POST /live/upstreams`), the
// VMs reach it through their reverse tunnel.
//
// Steps: the runners get a model catalog whose OpenRouter provider allow-list names no real provider
// (`POST /live/catalog`), so whatever provider OpenRouter routes to is outside it; E2E-06 (first task of a new
// private org on the platform device, answered by `deepseek/deepseek-v4.1-flash-1017`) plus one tool turn (Write);
// then a long turn the user interrupts (Stop → the session is `idle` again, 05-runner.md section 6.3) and follow-ups
// starting with `?`, `#` (multi-line) and `&` that real Claude Code must take as plain prompts (they are pasted,
// `agent_guest.delivery`). Asserted: the answers, the Write tool use and its file, the cost from OpenRouter's
// `usage.cost` (`cost_basis` provider), the provider recorded with the usage and audited as outside the allow-list
// (`model.provider_outside_allowlist`, B-13). Cost cap: `CostGuard` (USD 1.00 growth of the key's usage, fail closed, plus
// the cost the hub recorded, because OpenRouter books with a delay). Results: `<iteration>/live/live-01.json`.
import { expect, test } from '../fixtures/test.ts';
import { PLATFORM_MODEL, mainUsage } from '../fixtures/model.ts';
import { auditRows, sessionState } from '../fixtures/guest.ts';
import { mainSessions, onboard, platformDevice, poolReady, sendPrompt, taskBodies } from '../fixtures/platform.ts';
import { COST_CAP_USD, CostGuard, required, writeResult } from './helpers.ts';

const PROMPT = 'LIVE01: What is 2+2? Answer with the number only.';
const TOOL_PROMPT =
  'LIVE01: Use the Write tool to create the file live01.txt in the current directory with the content LIVE01-OK. ' +
  'Then reply with the single word done.';
const LONG_PROMPT =
  'LIVE01-LONG: Write a numbered list of 300 different animals, one per line, with one sentence about each. ' +
  'Do not use any tool.';
// Leading characters that would switch Claude Code's input into another mode if typed (05-runner.md section 6.2).
const PREFIXED = [
  {
    name: 'question',
    text: '? LIVE01-PREFIX: reply with the single word QMARK-OK and nothing else.',
    answer: 'QMARK-OK',
  },
  { name: 'hash', text: '# LIVE01-PREFIX\nReply with the single word HASH-OK and nothing else.', answer: 'HASH-OK' },
  { name: 'ampersand', text: '& LIVE01-PREFIX: reply with the single word AMP-OK and nothing else.', answer: 'AMP-OK' },
];
// A provider label OpenRouter never routes to: every real provider is outside this allow-list.
const ALLOW_LIST = ['LVT01 allow-list probe'];

async function followUp(page: import('@playwright/test').Page, text: string): Promise<void> {
  await page.getByTestId('chat.composer.prompt').fill(text);
  await page.getByTestId('chat.composer.send').click();
}

test.describe.configure({ mode: 'serial' });

test('@LIVE-01 @R-4.5 @R-4.6 @R-4.7 @R-14.4 @R-P.6 @live @needs-vm platform OpenRouter key with DeepSeek over the full chain', async ({
  page,
  orchestrator,
  authenticator,
  e2e,
}) => {
  test.setTimeout(1_500_000);
  const key = required('E2E_LIVE_OPENROUTER_KEY');
  await orchestrator.call('POST', '/live/catalog', { providers_allowed: ALLOW_LIST });
  const { base } = await orchestrator.call<{ base: string }>('POST', '/live/upstreams', { platform_key: key });
  // Over the cap: end the test and cut the runners off the provider (model upstreams back to the double).
  const guard = new CostGuard(base, key, async () => {
    await orchestrator.call('POST', '/live/model-double', {}).catch(() => undefined);
    await page.close();
  });
  const results: Record<string, unknown> = { test: 'LIVE-01', model: PLATFORM_MODEL, providers_allowed: ALLOW_LIST };
  results.usage_before = await guard.start();
  let taskCost = 0;
  try {
    const device = await platformDevice(orchestrator);
    await poolReady(orchestrator);
    await authenticator('platform');
    const onboarded = await onboard(page, orchestrator, 'Ada Live01');

    // E2E-06 against the real model: the first task of the new org, answered.
    const task = await sendPrompt(page, PROMPT);
    // the output item also holds its time stamp: match the answer's own paragraph
    const answer = page
      .getByTestId('chat.output')
      .locator('p')
      .filter({ hasText: /^\s*4\s*\.?\s*$/ });
    await expect(answer).toBeVisible({ timeout: 180_000 });
    const [session] = await mainSessions(orchestrator, task);
    expect(session).toMatchObject({ device_id: device, agent_kind: 'claude-code' });
    results.first_working_seconds = session!.seconds; // recorded; the < 10 s criterion is PERF-01's, not a live one

    // One tool turn in the same task.
    await followUp(page, TOOL_PROMPT);
    await expect(page.getByTestId('chat.tool.summary').filter({ hasText: /^Write/ })).toBeVisible({ timeout: 300_000 });
    await expect(page.getByTestId('chat.output').filter({ hasText: /done/i })).toBeVisible({ timeout: 300_000 });
    await expect
      .poll(async () => (await taskBodies(page, orchestrator, onboarded, task, 'session.file')).map((f) => f.path), {
        timeout: 60_000,
      })
      .toContain('live01.txt');

    // Interrupt (B-14): Stop while the long turn runs; the session must take prompts again (`idle`).
    await expect.poll(() => sessionState(orchestrator, session!.id), { timeout: 60_000 }).toBe('idle');
    await followUp(page, LONG_PROMPT);
    await expect.poll(() => sessionState(orchestrator, session!.id), { timeout: 120_000 }).toBe('working');
    await page.waitForTimeout(3_000);
    const stop = page.getByTestId('chat.control.stop');
    await expect(stop).toBeVisible({ timeout: 30_000 });
    const interruptedAt = Date.now();
    await stop.click();
    await expect.poll(() => sessionState(orchestrator, session!.id), { timeout: 60_000 }).toBe('idle');
    results.interrupt = { idle_after_ms: Date.now() - interruptedAt };

    // Prompts with a leading `?`, `#` (two lines) and `&` arrive as plain prompts (B-14).
    const prefixed: Record<string, boolean> = {};
    for (const item of PREFIXED) {
      await followUp(page, item.text);
      await expect(page.getByTestId('chat.output').filter({ hasText: item.answer })).toBeVisible({ timeout: 180_000 });
      await expect.poll(() => sessionState(orchestrator, session!.id), { timeout: 120_000 }).toBe('idle');
      prefixed[item.name] = true;
    }
    results.prefixed_prompts = prefixed;

    // Cost from OpenRouter's usage.cost, provider recorded and audited as outside the allow-list.
    await expect
      .poll(async () => (await mainUsage(orchestrator, task))?.requests ?? 0, { timeout: 60_000 })
      .toBeGreaterThanOrEqual(2);
    const usage = (await mainUsage(orchestrator, task))!;
    results.usage = usage;
    taskCost = usage.cost_micro_usd / 1_000_000;
    expect(usage).toMatchObject({ model_access: 'platform', model_id: PLATFORM_MODEL, cost_basis: 'provider' });
    expect(usage.cost_micro_usd).toBeGreaterThan(0);
    expect(usage.last_provider).toMatch(/^[!-~][ -~]*$/);
    expect(ALLOW_LIST).not.toContain(usage.last_provider);
    await expect(page.getByTestId('usage.cost')).toHaveAttribute('data-basis', 'provider');
    let audited: Awaited<ReturnType<typeof auditRows>> = [];
    await expect
      .poll(async () => (audited = await auditRows(orchestrator, task, 'model.provider_outside_allowlist')).length, {
        timeout: 60_000,
      })
      .toBeGreaterThan(0);
    results.provider_audit = audited.map((row) => ({ result: row.result, detail: row.detail }));
    expect(JSON.stringify(audited.map((row) => row.detail))).toContain(usage.last_provider!);
  } finally {
    let grown = Number.NaN;
    try {
      const spent = await guard.stop();
      grown = spent.grown;
      Object.assign(results, { usage_after: spent.after, key_cost_usd: spent.grown });
    } catch (error) {
      results.usage_after_error = (error as Error).message;
    }
    results.hub_recorded_cost_usd = taskCost;
    results.cap_exceeded = guard.exceeded;
    results.guard_reason = guard.reason;
    writeResult(e2e.iterationDir, 'live-01', results);
    expect(guard.exceeded, 'cost cap of the live run').toBe(false);
    expect(Number.isNaN(grown), 'the key usage after the run is readable').toBe(false);
    expect(Math.max(grown, taskCost), 'cost cap of the live run').toBeLessThanOrEqual(COST_CAP_USD);
  }
});
