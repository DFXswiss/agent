// E2E-50 (ADR-067; 11-agent-access.md sections 5.4 and 8.4): pairing an agent that runs on another computer. The
// agent tool starts the pairing with `pair --remote` and hands the user only a link and a code (no loopback listener,
// no browser stand-in opens anything). Ada's page opens the link: the warning names the other computer, a wrong code
// keeps "Confirm with passkey" disabled, the right code (typed in lower case with a space) and the passkey confirm and
// the page shows the return code. `pair --wait` sees the confirmation, `pair --wait --return-code <code>` connects:
// the agent acts for Ada, the page reports "connected", the settings mark the main orchestrator "another computer",
// the shell shows the banner and the notification `agent_paired_remote` is there. A second remote pairing (which
// replaces the first agent) ends with `pairing_failed` when the agent is given a wrong return code: nothing is
// claimed and the pending pairing is gone. Runs on T1.
import { expect, test } from '../fixtures/test.ts';
import { anonymousBudget, COST, personOf, visit } from '../fixtures/identity.ts';
import { agentTool, agentToolRun } from '../fixtures/agent-tool.ts';
import { uuid } from '../fixtures/platform.ts';
import type { Page } from '@playwright/test';
import type { Orchestrator } from '../fixtures/orchestrator.ts';

const CODE = /^[0-9A-Z]{5}-[0-9A-Z]{5}$/;
const RETURN_CODE = /^[0-9A-Z]{4}-[0-9A-Z]{4}$/;

/** `code` with its first character changed (still a well-formed code). */
const wrong = (code: string) => (code.startsWith('0') ? `1${code.slice(1)}` : `0${code.slice(1)}`);

type Json = Record<string, unknown>;

/** `pair --remote` on the computer `home`: the link and the agent code, checked against ADR-067. */
async function startRemote(orchestrator: Orchestrator, hub: string, home: string, host: string): Promise<Json> {
  await anonymousBudget(2 * COST.lookup);
  const started = await agentTool(orchestrator, home, [
    'pair',
    '--hub',
    hub,
    '--remote',
    '--name',
    'Claude Code',
    '--host',
    host,
  ]);
  expect(started).toMatchObject({ ok: true, state: 'waiting', hub });
  const link = new URL(started.connect_link as string);
  expect(`${link.origin}${link.pathname}`).toBe(`${hub}/connect`);
  const fragment = new URLSearchParams(link.hash.slice(1));
  expect([...fragment.keys()]).toEqual(['p', 'f', 'm', 'a', 'h']);
  expect([fragment.get('m'), fragment.get('a'), fragment.get('h')]).toEqual(['remote', 'Claude Code', host]);
  expect(started.code).toMatch(CODE);
  return { ...started, pairing: fragment.get('p') };
}

/** Opens the link on Ada's page, refuses a wrong code, types the right one and confirms; returns the return code. */
async function confirmOnPage(page: Page, started: Json, host: string, replaces: boolean): Promise<string> {
  const code = started.code as string;
  await anonymousBudget(COST.page + COST.lookup + COST.signIn);
  await page.goto('about:blank');
  await page.goto(started.connect_link as string);
  await expect(page).toHaveURL(/\/connect$/);
  await expect(page.getByTestId('connect.request')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('connect.remote-warning')).toContainText(
    `This AI agent runs on another computer (${host}).`,
  );
  await expect(page.getByTestId('connect.warning')).toHaveCount(0);
  expect(await page.locator('body').innerText()).not.toContain(code);
  await expect(page.getByTestId('connect.confirm')).toBeDisabled();
  await page.getByTestId('connect.code.input').fill(wrong(code));
  await expect(page.getByTestId('connect.code')).toContainText('This is not the code of this request.');
  await expect(page.getByTestId('connect.confirm')).toBeDisabled();
  await page.getByTestId('connect.code.input').fill(code.toLowerCase().replace('-', ' '));
  await expect(page.getByTestId('connect.confirm')).toBeEnabled();
  await page.getByTestId('connect.confirm').click();
  if (replaces) {
    await expect(page.getByTestId('connect.replace')).toBeVisible({ timeout: 30_000 });
    await page.getByTestId('connect.replace.confirm').click();
  }
  const returned = page.getByTestId('connect.return-code');
  await expect(returned).toHaveText(RETURN_CODE, { timeout: 30_000 });
  await expect(page.getByTestId('connect.outcome.waiting')).toBeVisible();
  return (await returned.textContent())!.trim();
}

test('@E2E-50 an agent on another computer pairs with the codes of both sides and a wrong return code ends it', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, 'Ada Remote');
  const hub = e2e.hubOrigin;

  // 1. The agent starts the pairing: link and code, nothing opened on this computer; the pairing is still open.
  const first = await startRemote(orchestrator, hub, 'server', 'build-server');
  await anonymousBudget(COST.lookup);
  expect(await agentTool(orchestrator, 'server', ['pair', '--wait', '--timeout', '0'])).toMatchObject({
    state: 'waiting',
  });
  const again = await agentToolRun(orchestrator, 'server', ['pair', '--hub', hub, '--remote']);
  expect(again.result).toMatchObject({ ok: false, error: 'pairing_pending' });
  expect(again.opened).toEqual([]);

  // 2. Ada's page: warning, wrong code refused, right code and passkey confirm; the page shows the return code.
  const back = await confirmOnPage(page, first, 'build-server', false);
  await anonymousBudget(COST.lookup);
  expect(await agentTool(orchestrator, 'server', ['pair', '--wait', '--timeout', '30'])).toMatchObject({
    state: 'confirmed',
  });

  // 3. The agent connects with the return code and acts for Ada.
  await anonymousBudget(2 * COST.lookup);
  const connected = await agentTool(orchestrator, 'server', ['pair', '--wait', '--return-code', back]);
  expect(connected).toMatchObject({ ok: true, state: 'connected', kind: 'pair', hub });
  await expect(page.getByTestId('connect.outcome.connected')).toBeVisible({ timeout: 30_000 });
  expect(await agentTool(orchestrator, 'server', ['status'])).toMatchObject({ user_id: ada.user.userId });
  const firstId = first.pairing as string;
  expect(
    await orchestrator.hubQuery<{ state: string }>(`SELECT state FROM hub.agent WHERE id = '${uuid(firstId)}'`),
  ).toEqual([{ state: 'active' }]);
  const pending = await agentToolRun(orchestrator, 'server', ['pair', '--wait']);
  expect(pending.result).toMatchObject({ ok: false, error: 'no_pending_pairing' });

  // 4. The notification, the settings marker and the shell banner.
  expect(
    await orchestrator.hubQuery<{ kind: string; ref: string }>(
      `SELECT kind, ref FROM hub.notification WHERE recipient_id = '${uuid(ada.user.userId)}'
          AND ref LIKE 'agent_paired_remote:%'`,
    ),
  ).toEqual([{ kind: 'agent.account_change', ref: `agent_paired_remote::${firstId}` }]);
  await visit(page, '/notifications');
  const banner = page.getByTestId('shell.remote-agent');
  await expect(banner).toContainText('Your main orchestrator runs on another computer (build-server), connected');
  await expect(
    page
      .locator('[data-testid="notifications.item"][data-kind="agent.account_change"]')
      .filter({ hasText: 'An AI agent on another computer (“Claude Code”) became your main orchestrator' }),
  ).toHaveCount(1);
  await visit(page, '/settings');
  const item = page.locator(`[data-testid="agents.item"][data-agent="${firstId}"]`);
  await expect(item).toHaveAttribute('data-state', 'active');
  await expect(item.getByTestId('agents.item.remote')).toHaveText('Runs on another computer');
  await expect(item.getByTestId('agents.item.host')).toContainText('build-server');
  await expect(banner).toBeVisible();

  // 5. A second remote pairing replaces the first agent; a wrong return code ends it without a claim.
  const second = await startRemote(orchestrator, hub, 'other', 'other-server');
  const secondBack = await confirmOnPage(page, second, 'other-server', true);
  await anonymousBudget(2 * COST.lookup);
  const failed = await agentToolRun(orchestrator, 'other', ['pair', '--wait', '--return-code', wrong(secondBack)]);
  expect(failed.rc).toBe(1);
  expect(failed.result).toMatchObject({ ok: false, error: 'pairing_failed', reason: 'return_code_mismatch' });
  expect(
    await orchestrator.hubQuery<{ state: string; claimed: boolean }>(
      `SELECT state, claimed_at IS NOT NULL AS claimed FROM hub.agent WHERE id = '${uuid(second.pairing as string)}'`,
    ),
  ).toEqual([{ state: 'confirmed', claimed: false }]);
  const gone = await agentToolRun(orchestrator, 'other', ['pair', '--wait', '--return-code', secondBack]);
  expect(gone.result).toMatchObject({ ok: false, error: 'no_pending_pairing' });
  expect((await agentToolRun(orchestrator, 'other', ['status'])).result).toMatchObject({
    ok: false,
    error: 'not_paired',
  });
  // the first agent was replaced by the confirmation
  expect((await agentToolRun(orchestrator, 'server', ['status'])).result).toMatchObject({
    ok: false,
    error: 'agent_revoked',
  });
});
