// E2E-28 (07-testing.md section 2.1; 05-runner.md sections 11.2, 11.3; ADR-011): connecting a Claude subscription on
// the platform device with the login doubles (login-CLI double as `claude setup-token`, auth-page double over HTTPS,
// mail double with IMAPS; `orchestrator/routes/login.py`). (a) Setup-token relay through the web: the device sends
// the authorize link, the person authorizes in their own browser (magic link from their INBOX) and pastes the code;
// the device stores the `claude_subscription` credential. (b) Mailbox automation: with a stored mailbox the device
// drives the auth pages headless, finds the magic link in the Junk folder and stores the credential with the e-mail
// address. (c) Failure path: the auth page asks for a human check, the automation gives up and the flow falls back to
// the relay (`login_automation_failed`), which completes. After every login the device's temporary login directory is
// gone; no token, code or mailbox password leaks (M2c scan). No VM needed. T1 only.
import { expect, test } from '../fixtures/test.ts';
import {
  expectNoCredentialLeak,
  finishRelay,
  loginDirs,
  loginDoubles,
  openSettings,
  randomText,
  startLogin,
  storeMailbox,
} from '../fixtures/model.ts';
import { onboard, platformDevice } from '../fixtures/platform.ts';

interface AuthState {
  readonly ok: boolean;
  readonly failures: readonly string[];
  readonly milestones: readonly string[];
}

test.describe.configure({ mode: 'serial' });

// E2E-28 is a T1 test (07-testing.md section 2.1): the T2 run leaves it out.
test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-28 is a T1 test (07-testing.md section 2.1)');
});

test('@E2E-28 @R-4.2 @R-4.4 @R-P.5 the subscription login stores the credential through the relay and the mailbox automation and falls back to the relay', async ({
  page,
  browser,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(600_000);
  const tag = randomText(4);
  const seeds = { relay: `e2e28-relay-${tag}`, auto: `e2e28-auto-${tag}`, fallback: `e2e28-fallback-${tag}` };
  const relayMailbox = { email: `relay-${tag}@example.test`, password: randomText() };
  const mailbox = { email: `subscriber-${tag}@example.test`, password: randomText() };
  await platformDevice(orchestrator);
  await authenticator('platform');
  await onboard(page, orchestrator, 'Ada E2E28');
  const credentialVersion = async () => {
    const rows = await orchestrator.hubQuery<{ version: number }>(
      `SELECT c.version FROM hub.credential c WHERE c.kind = 'claude_subscription' AND c.owner_id =
         (SELECT r.user_id FROM hub.device_request r WHERE r.kind = 'claude.login' ORDER BY r.created_at DESC LIMIT 1)`,
    );
    return rows[0]?.version ?? 0;
  };
  const authState = async () =>
    (await orchestrator.doubleState('authpage')) as unknown as AuthState & Record<string, unknown>;

  // (a) Relay: link → the person's browser (magic link in INBOX) → code pasted → device stores the token.
  const doubles = await loginDoubles(orchestrator, { seed: seeds.relay, account: relayMailbox });
  expect(doubles.pages).toMatch(/^https:\/\/127\.0\.0\.1:\d+$/);
  await startLogin(page, 'manual');
  // While the login CLI waits for the code, its temporary home exists below the runner's home ...
  await expect(page.getByTestId('claude.authorize.open')).toBeVisible({ timeout: 60_000 });
  expect(await loginDirs(orchestrator)).toHaveLength(1);
  await finishRelay(page, browser, orchestrator, relayMailbox, 'INBOX');
  await expect(page.getByTestId('credentials.item.claude_subscription')).toBeVisible();
  // ... and after the token is stored it is gone.
  expect(await credentialVersion()).toBe(1);
  expect(await loginDirs(orchestrator)).toEqual([]);
  expect((await authState()).milestones).toEqual(
    expect.arrayContaining(['email_page', 'mail_sent', 'signed_in', 'authorized', 'code_page', 'token']),
  );

  // (b) Mailbox automation, magic link in Junk: no human step; the device reports the address it signed in with.
  await loginDoubles(orchestrator, { seed: seeds.auto, junk: true, account: mailbox });
  await storeMailbox(page, doubles, mailbox);
  await startLogin(page, 'automatic');
  await expect(page.getByTestId('claude.connected')).toContainText(mailbox.email, { timeout: 180_000 });
  await expect(page.getByTestId('claude.automation-failed')).toHaveCount(0);
  await expect(page.getByTestId('credentials.item.claude_subscription.detail')).toContainText(mailbox.email);
  expect(await credentialVersion()).toBe(2);
  expect(await loginDirs(orchestrator)).toEqual([]);
  const automated = await authState();
  expect(automated.milestones).toEqual(expect.arrayContaining(['mail_sent', 'signed_in', 'authorized', 'token']));
  const junk = await orchestrator.doubleRequest(
    'mail',
    'GET',
    `/_double/messages?user=${encodeURIComponent(mailbox.email)}&folder=Junk`,
  );
  expect(JSON.stringify(junk.body)).toContain('Secure link to log in to Claude');

  // (c) Failure path: a human-verification page stops the automation; the relay link follows and completes.
  await loginDoubles(orchestrator, { seed: seeds.fallback, fault: 'email_page' });
  await startLogin(page, 'automatic');
  await expect(page.getByTestId('claude.automation-failed')).toHaveAttribute('data-code', 'login_automation_failed', {
    timeout: 180_000,
  });
  await loginDoubles(orchestrator, { seed: seeds.fallback }); // the person's own browser is no bot
  expect(await loginDirs(orchestrator)).toHaveLength(1); // the relay continues in the same temporary home
  await finishRelay(page, browser, orchestrator, mailbox, 'INBOX');
  expect(await credentialVersion()).toBe(3);
  expect(await loginDirs(orchestrator)).toEqual([]);
  expect((await authState()).ok).toBe(true);
  await openSettings(page);
  await expect(page.getByTestId('credentials.item.claude_subscription')).toBeVisible();

  const requests = await orchestrator.hubQuery<{ state: string; n: number }>(
    `SELECT state, count(*)::int AS n FROM hub.device_request WHERE kind = 'claude.login' GROUP BY state ORDER BY state`,
  );
  expect(requests.every((row) => row.state === 'answered')).toBe(true);
  await expectNoCredentialLeak(
    orchestrator,
    [relayMailbox.password, mailbox.password],
    [seeds.relay, seeds.auto, seeds.fallback],
  );
});
