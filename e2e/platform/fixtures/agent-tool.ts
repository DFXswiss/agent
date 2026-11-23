// The user's agent tool `agent-platform` on the orchestrator's machine (orchestrator/routes/agent_tool.py): each
// `home` is one user's computer. `pairAgent` runs `agent-platform pair`, opens the link the tool handed to its browser
// stand-in in the user's page and confirms with the passkey; the page then hands the secret over to the tool's
// loopback listener (T1: browser and tool on one machine). `agentTool` runs any other command and returns the JSON
// object it printed.
import { expect, type Page } from '@playwright/test';
import { anonymousBudget, COST } from './identity.ts';
import type { Orchestrator } from './orchestrator.ts';

export interface ToolRun {
  readonly done: boolean;
  readonly rc: number | null;
  readonly result: Record<string, unknown> | null;
  readonly stderr_tail: string;
  readonly opened: string[];
}

const start = (orchestrator: Orchestrator, home: string, args: readonly string[]) =>
  orchestrator.call<{ id: string }>('POST', '/agent-tool', { args, home });

const poll = (orchestrator: Orchestrator, id: string) => orchestrator.call<ToolRun>('GET', `/agent-tool/${id}`);

async function finished(orchestrator: Orchestrator, id: string, timeout: number): Promise<ToolRun> {
  await expect.poll(async () => (await poll(orchestrator, id)).done, { timeout, intervals: [500] }).toBe(true);
  return poll(orchestrator, id);
}

/** `agent-platform <args>` on the computer `home`; must exit 0. Returns the printed JSON object. */
export async function agentTool(
  orchestrator: Orchestrator,
  home: string,
  args: readonly string[],
  timeout = 60_000,
): Promise<Record<string, unknown>> {
  const run = await finished(orchestrator, (await start(orchestrator, home, args)).id, timeout);
  expect(run.rc, `agent-platform ${args.join(' ')}: ${run.stderr_tail}`).toBe(0);
  return run.result ?? {};
}

/** Pairs the agent of `home` with the account signed in on `page` (passkey gesture on `/connect`). */
export async function pairAgent(
  orchestrator: Orchestrator,
  page: Page,
  hubOrigin: string,
  home: string,
): Promise<Record<string, unknown>> {
  // the tool's anonymous requests (config, pairing) and the page's (config, ceremony)
  await anonymousBudget(2 * COST.lookup + COST.page + COST.signIn);
  const { id } = await start(orchestrator, home, [
    'pair',
    '--hub',
    hubOrigin,
    '--name',
    'Claude Code',
    '--host',
    'E2E-Mac',
  ]);
  await expect
    .poll(async () => (await poll(orchestrator, id)).opened.length, { timeout: 30_000, intervals: [250] })
    .toBeGreaterThan(0);
  const [link] = (await poll(orchestrator, id)).opened;
  expect(link!.startsWith(`${hubOrigin}/connect#`), link).toBe(true);
  await page.goto(link!);
  await expect(page.getByTestId('connect.request')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('connect.confirm').click();
  await expect(page).toHaveURL(/\/settings\?agent=connected$/, { timeout: 60_000 });
  const run = await finished(orchestrator, id, 60_000);
  expect(run.rc, `agent-platform pair: ${run.stderr_tail}`).toBe(0);
  return run.result ?? {};
}
