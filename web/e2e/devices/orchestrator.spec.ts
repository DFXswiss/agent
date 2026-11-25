// A running session on DFX hardware as the user's main orchestrator (11-agent-access.md section 11, ADR-082 sections 6
// and 9.6-9.9) against the real hub, the session's device played by the device double: "Make this session my main
// orchestrator" only on the owner's running main session (not on a subagent, not once it stopped); the dialog names
// the session, its device and the main orchestrator it replaces, waits for the keys the device registers, checks the
// device's signature under the device key of the verified platform log (a hub that shows another key's signature is
// refused with `pairing_mismatch`), runs the pairing-tagged passkey ceremony (again after a cancelled one and after
// `pairing_gesture_required`), confirms, sends the handover signed by the user and waits for the session's claim; the
// replaced agent is revoked at once, the settings name the session "On DFX hardware", and the session's end revokes it
// (a confirmation revoked before the session claims fails).
import type { Page } from '@playwright/test';
import { orchestratorHandoverText, verifyText } from '../../src/core/crypto/agent';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { expect, test } from '../fixtures/test.ts';
import { catchHandover, TestAgent } from '../helpers/agent.ts';
import { mainSession, orchestratorView, registerOrchestrator, walk } from '../helpers/director.ts';
import { navigateInApp } from '../helpers/onboard.ts';
import { childSession } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

/** The session's device registers the keys once the owner's request is recorded (the runner acts on the hint). */
async function registered(session: string, drawn: Awaited<ReturnType<typeof TestAgent.session>>, forged = false) {
  await expect
    .poll(async () => (await registerOrchestrator(session, drawn, forged)).status, { timeout: 20_000 })
    .toBe(200);
}

/** The handover the runner fetches for the session (`{ct, sig}`) and the task owner. */
async function storedHandover(session: string): Promise<{ ct: string; sig: string; userId: string }> {
  let found: { ct: string; sig: string; userId: string } | null = null;
  await expect
    .poll(async () => {
      const { body } = await orchestratorView(session);
      const view = body as { user_id: string; pairing: { handover: { ct: string; sig: string } | null } | null };
      if (view.pairing?.handover) found = { ...view.pairing.handover, userId: view.user_id };
      return found !== null;
    })
    .toBe(true);
  return found!;
}

/** A task with its main session `working` on the user's device; the page stays on the task. */
async function runningTask(page: Page, prompt: string): Promise<string> {
  const task = await sendPrompt(page, prompt);
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  return main.session_id;
}

test('makes a running main session the main orchestrator with the passkey; it replaces the agent and ends with the session', async ({
  page,
  baseURL,
}) => {
  const origin = baseURL!;
  const ada = await freshUser(page, origin, { poolReady: 2 });
  const user = await PageUser.open(page, ada.recoveryCode);
  const session = await runningTask(page, 'Orchestrate the release');
  const child = await childSession(session);

  // Ada's agent on her computer is her main orchestrator first
  const first = await TestAgent.pair(origin);
  const handover = catchHandover(page, origin, async (sent) => {
    expect((await first.claim(sent.fields.get('r')!, sent.fields.get('u')!)).status).toBe(200);
  });
  await page.goto(`/connect#${await first.fragment()}`);
  await page.getByTestId('connect.confirm').click();
  await handover;
  await expect(page).toHaveURL(/\/login\?next=/);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('agents.hint.connected')).toBeVisible();

  // a subagent session is never offered
  await navigateInApp(page, `/o/${ada.org}/s/${child}`);
  await expect(page.getByTestId('session.title')).toContainText('Subagent 1');
  await expect(page.getByTestId('session.tabs')).toBeVisible();
  await expect(page.getByTestId('orchestrator.make')).toHaveCount(0);

  await navigateInApp(page, `/o/${ada.org}/s/${session}`);
  await page.getByTestId('orchestrator.make').click();
  const dialog = page.getByTestId('orchestrator.dialog');
  await expect(dialog.getByTestId('orchestrator.session')).toHaveText('This session');
  await expect(dialog.getByTestId('orchestrator.device')).toHaveText(/without a name$/);
  await expect(dialog.getByTestId('orchestrator.access')).toHaveText(
    'This session receives full access to your account and can do everything you can.',
  );
  await expect(dialog.getByTestId('orchestrator.key')).toContainText('never leaves it');

  // the session draws its keys; its device signs their fingerprint and registers them
  const drawn = await TestAgent.session(origin);
  await registered(session, drawn);
  await expect(dialog.getByTestId('orchestrator.preparing')).toHaveCount(0);
  await expect(dialog.getByTestId('orchestrator.replaces')).toHaveText(
    'Replaces “Claude Code”, your current main orchestrator. “Claude Code” is disconnected at once.',
  );
  await expect(dialog.getByTestId('orchestrator.replaces')).toHaveAttribute('data-agent', first.id);
  await dialog.getByTestId('orchestrator.confirm').click();
  await expect(dialog.getByTestId('orchestrator.outcome.waiting')).toHaveText('Confirmed. Waiting for the session…');
  await expect(dialog.getByTestId('orchestrator.confirm')).toHaveCount(0);

  // the replaced agent is revoked at once; the handover is signed by Ada and opens only with the session's key
  expect((await first.request('GET', '/v1/agent/me')).status).toBe(401);
  const stored = await storedHandover(session);
  expect(stored.userId).toBe(user.userId);
  await verifyText(
    b64uDecode(user.identity.public().sig, 32),
    stored.sig,
    orchestratorHandoverText(stored.ct, drawn.agent.id, session),
  );
  expect((await drawn.agent.claimSession(stored.ct, user.userId)).status).toBe(200);
  await expect(dialog.getByTestId('orchestrator.outcome.connected')).toHaveText(
    'This session is now your main orchestrator.',
  );
  await dialog.getByTestId('orchestrator.close').click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId('orchestrator.current')).toHaveText('This session is your main orchestrator.');
  await expect(page.getByTestId('orchestrator.make')).toHaveCount(0);

  // the session's agent is Ada's agent in the log and acts with its own key
  expect((await user.log(ada.org)).state.activeAgent(user.userId)?.id).toBe(drawn.agent.id);
  expect((await drawn.agent.request('GET', `/v1/orgs/${ada.org}`)).status).toBe(200);
  await navigateInApp(page, '/settings');
  const item = page.locator(`[data-testid="agents.item"][data-agent="${drawn.agent.id}"]`);
  await expect(page.getByTestId('agents.main').locator('[data-testid="agents.item"]')).toHaveAttribute(
    'data-agent',
    drawn.agent.id,
  );
  await expect(item.getByTestId('agents.item.session')).toHaveText(
    'On DFX hardware, in a session of one of your tasks',
  );
  await expect(item.getByTestId('agents.item.session')).toHaveAttribute('data-session', session);
  await expect(item.getByTestId('agents.item.runs')).toHaveCount(0);
  await expect(item.getByTestId('agents.item.remote')).toHaveCount(0);
  await expect(item.getByTestId('agents.item.replaces')).toHaveText('Replaced Claude Code');
  await expect(page.locator(`[data-testid="agents.item"][data-agent="${first.id}"]`)).toHaveAttribute(
    'data-state',
    'revoked',
  );

  // the session ends: its orchestrator is revoked with it
  await walk(session, ['stopping', 'stopped']);
  await expect.poll(async () => (await drawn.agent.request('GET', '/v1/agent/me')).status).toBe(401);
  // a stopped session is not offered (in-app navigation: a page load would drop the browser coverage so far)
  await navigateInApp(page, `/o/${ada.org}/s/${session}`);
  await expect(page.getByTestId('session.tabs')).toBeVisible();
  await expect(page.getByTestId('orchestrator.make')).toHaveCount(0);
  await expect(page.getByTestId('orchestrator.current')).toHaveCount(0);
  await navigateInApp(page, '/settings');
  await expect(item).toHaveAttribute('data-state', 'revoked');
  await expect(page.getByTestId('agents.main.none')).toBeVisible();
});

test('refuses keys the device did not sign, asks for the gesture again and fails when revoked before the claim', async ({
  page,
  baseURL,
}) => {
  const origin = baseURL!;
  const ada = await freshUser(page, origin, { poolReady: 2 });
  const forgedSession = await runningTask(page, 'Check the keys');

  // the hub shows a signature of another key over the session's keys: the browser refuses before any gesture
  await navigateInApp(page, `/o/${ada.org}/s/${forgedSession}`);
  await page.getByTestId('orchestrator.make').click();
  const dialog = page.getByTestId('orchestrator.dialog');
  await expect(dialog.getByTestId('orchestrator.replaces')).toHaveCount(0);
  await registered(forgedSession, await TestAgent.session(origin), true);
  await expect(dialog.getByTestId('orchestrator.error')).toHaveAttribute('data-code', 'pairing_mismatch');
  await expect(dialog.getByTestId('orchestrator.confirm')).toHaveCount(0);
  await expect(dialog.getByTestId('orchestrator.close')).toHaveText('Cancel');
  await dialog.getByTestId('orchestrator.close').click();
  await expect(dialog).toHaveCount(0);

  // another running session: a cancelled ceremony, then one that lost its pairing tag, then the confirmation
  await navigateInApp(page, `/o/${ada.org}`);
  const session = await runningTask(page, 'Second try');
  await navigateInApp(page, `/o/${ada.org}/s/${session}`);
  await page.getByTestId('orchestrator.make').click();
  const drawn = await TestAgent.session(origin);
  await registered(session, drawn);
  await expect(dialog.getByTestId('orchestrator.confirm')).toBeEnabled();
  await page.evaluate(() => {
    const credentials = navigator.credentials;
    const original = credentials.get.bind(credentials);
    let left = 1;
    credentials.get = (options?: CredentialRequestOptions) => {
      if (left-- > 0) return Promise.reject(new DOMException('The operation was not allowed.', 'NotAllowedError'));
      return original(options);
    };
  });
  await dialog.getByTestId('orchestrator.confirm').click();
  await expect(dialog.getByTestId('orchestrator.notice')).toHaveText('Not confirmed. Try again when you are ready.');
  let stripped = 0;
  await page.route('**/v1/passkeys/login/begin', async (route) => {
    if (stripped > 0) return route.fallback();
    stripped += 1;
    return route.fallback({ postData: '{}' });
  });
  await dialog.getByTestId('orchestrator.confirm').click();
  await expect(dialog.getByTestId('orchestrator.notice')).toHaveText(
    'The confirmation took too long. Confirm once more with your passkey.',
  );
  await dialog.getByTestId('orchestrator.confirm').click();
  await expect(dialog.getByTestId('orchestrator.outcome.waiting')).toBeVisible();
  expect(stripped).toBe(1);
  await storedHandover(session);

  // Ada disconnects the confirmed agent elsewhere before the session claims: the dialog says it failed
  const user = await PageUser.open(page, ada.recoveryCode);
  await user.request('DELETE', `/v1/me/agents/${drawn.agent.id}`);
  await expect(dialog.getByTestId('orchestrator.outcome.failed')).toHaveText(
    'The session did not complete the connection. Try again.',
  );
  await expect(dialog.getByTestId('orchestrator.close')).toHaveText('Close');
});
