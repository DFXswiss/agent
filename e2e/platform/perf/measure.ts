// Shared steps of the PERF specs (07-testing.md sections 2.2 and 5.3): the scenario with one answer per run, one
// measured first task in a fresh browser profile and new private org, and the series evaluation by the orchestrator
// (`POST /perf/time-to-working`: hub-clock values from the hub database, rows appended to
// `<artifacts>/perf-time-to-working.csv`).
import { readFileSync } from 'node:fs';
import type { Browser } from '@playwright/test';
import { expect } from '../fixtures/test.ts';
import { addAuthenticator } from '../fixtures/authenticator.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { onboard, sendPrompt, type Onboarded } from '../fixtures/platform.ts';

/** Runs of the series "new org" (PERF-01): `E2E_PERF_RUNS` (acceptance of ACC-M1/PFM-01: 20), else 5. */
export const RUNS = Number(process.env.E2E_PERF_RUNS ?? '5');
/** Runs of the series "new workgroup" (PERF-01) and of PERF-02: `E2E_PERF_SHORT_RUNS` (acceptance: 10), else `RUNS`. */
export const SHORT_RUNS = Number(process.env.E2E_PERF_SHORT_RUNS ?? String(RUNS));
export const PROMPT = 'What is 2+2?';
/** Start estimates of the scheduler for a device without a VM of the workgroup (03-api.md section 4.6.2). */
export const POOL_ESTIMATE_MS = 3000;
export const CLONE_ESTIMATE_MS = 8000;

export function scenario(runs: number): unknown {
  const base = JSON.parse(readFileSync(new URL('./time-to-working.json', import.meta.url), 'utf8')) as {
    steps: Record<string, unknown>[];
  };
  return { ...base, steps: [{ ...base.steps[0], repeat: runs }] };
}

export interface Series {
  readonly summary: { runs: number; p95_s: number | null; max_s: number | null; passed: boolean };
  readonly runs: {
    task_id: string;
    total_s: number | null;
    estimate_ms: number | null;
    slowest: [string, number] | null;
  }[];
  readonly csv: string;
}

/** `limit` (s): the bound of the series' `passed` (default: the 10 s of PERF-01/02). */
export async function evaluate(
  orchestrator: Orchestrator,
  label: string,
  tasks: string[],
  limit?: number,
): Promise<Series> {
  const series = await orchestrator.call<Series>('POST', '/perf/time-to-working', {
    label,
    tasks,
    ...(limit !== undefined && { limit }),
  });
  console.log(`${label}: ${JSON.stringify(series)}`);
  return series;
}

/** One measured run: a new browser profile onboards into a new private org and sends the first task. */
export async function firstTaskInNewOrg(
  browser: Browser,
  origin: string,
  orchestrator: Orchestrator,
  name: string,
): Promise<{ task: string; onboarded: Onboarded }> {
  const context = await browser.newContext({ baseURL: origin });
  try {
    const page = await context.newPage();
    await addAuthenticator(page, 'platform');
    const onboarded = await onboard(page, orchestrator, name);
    const task = await sendPrompt(page, PROMPT);
    await expect(page.getByTestId('countdown.summary')).toContainText('Ready in', { timeout: 60_000 });
    return { task, onboarded };
  } finally {
    await context.close();
  }
}
