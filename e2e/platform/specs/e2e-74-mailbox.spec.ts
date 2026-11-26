// E2E-74 (KOR-74, ADR-099; 05-runner.md section 12.2; 07-testing.md section 2.1): a session reads a mailbox through
// the platform - no work VM, no password in the session.
// - Ada adds the mail double's mailbox in the settings section "Mailboxes" (name `signup-kz74`, the password typed
//   once, sealed in her browser) and releases it to her default workgroup.
// - Her session asks `mail.find` for the sign-in mail with a wait of four minutes; the device opens the mailbox
//   itself over IMAPS and looks every five seconds. The mail is sent only once the operation runs at the hub, so the
//   answer proves the wait on the device; the session answers with the link it read.
// - The hub has the operation `mail.find` (succeeded) and no `workvm.request`; the audit row `mail.read` names the
//   mailbox and one mail; the password is nowhere in the VMs, the runner device or the hub (credential scan), and the
//   link and the answer reach the hub only encrypted. Runs on T1.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext } from '../fixtures/guest.ts';
import { hubOperations } from '../fixtures/github.ts';
import { expectNoCredentialLeak, loginDoubles, openSettings, randomText } from '../fixtures/model.ts';
import { expectAnswerInChat, home, onboard, platformDevice, sendPrompt, uuid } from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-74-mailbox.json', import.meta.url), 'utf8'),
);
const NAME = 'signup-kz74';
const ADDRESS = 'signup-kz74@example.org';
const PROMPT = 'E2E74-MAIL kz74-prompt-c5t1: sign in with the new account and read the link of the sign-in mail';
const LINK = 'https://example.com/magic-link#kz74-link-q8w3';
const CANARIES = ['kz74-prompt-c5t1', 'kz74-link-q8w3', 'kz74-answer-m2p6'];

test.describe.configure({ mode: 'serial' });

test('@E2E-74 @needs-vm a session finds the sign-in mail of a double with mail.find and reads its link, without a VM of its own and without the password', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-74 runs on T1 (07-testing.md section 2.1)');
  test.setTimeout(900_000);
  const password = `kz74-pw-${randomText(8)}`;
  await orchestrator.loadScenario('model', scenario);
  await platformDevice(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz74-name-r3d8');
  const doubles = await loginDoubles(orchestrator, {
    seed: `kz74-${randomText(4)}`,
    account: { email: ADDRESS, password },
  });
  const [wg] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT id::text FROM hub.wg WHERE org_id = '${uuid(onboarded.org)}' AND is_default`,
  );

  // The mailbox, added and released in the settings.
  await openSettings(page);
  const section = page.getByTestId('mailboxes.section');
  await section.scrollIntoViewIfNeeded();
  await section.getByTestId('mailboxes.add').click();
  const fields: Record<string, string> = {
    name: NAME,
    purpose: 'Sign-in mails of the new account',
    email: ADDRESS,
    imap_host: doubles.imaps.host,
    imap_port: String(doubles.imaps.port),
    imap_user: ADDRESS,
    imap_password: password,
  };
  for (const [field, value] of Object.entries(fields)) await section.getByTestId(`mbform.${field}`).fill(value);
  await section.getByTestId('mbform.save').click();
  const card = section.locator(`[data-testid^="mbcard."][data-name="${NAME}"]`);
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.getByTestId('mbcard.release').click();
  await card.getByTestId(`mbrelease.wg.${wg!.id}`).check();
  await card.getByTestId('mbrelease.save').click();
  await expect(card.getByTestId('mbrelease.saved')).toBeVisible({ timeout: 30_000 });
  await expect(card.getByTestId(`mbcard.released.${wg!.id}`)).toBeVisible();
  const mirrors = await orchestrator.hubQuery<{ wgs: string[] }>(
    `SELECT u.wgs FROM hub.mailbox_use u JOIN hub.credential c ON c.id = u.credential_id WHERE c.kind = 'mailbox'`,
  );
  expect(mirrors).toEqual([{ wgs: [wg!.id] }]);

  // The session asks; the mail is sent while the device waits for it.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expect
    .poll(async () => (await hubOperations(orchestrator, task)).some((op) => op.type === 'mail.find'), {
      timeout: 600_000,
    })
    .toBe(true);
  await orchestrator.call('POST', '/mail/deliver', {
    to: ADDRESS,
    from: 'Sign-in <no-reply@mail.example.com>',
    subject: 'Sign in to your new account',
    text: `Click to sign in: ${LINK}`,
  });
  await expectAnswerInChat(page, `Sign-in link found: ${LINK} (kz74-answer-m2p6).`);

  const operations = await hubOperations(orchestrator, task);
  expect(operations.filter((op) => op.type === 'mail.find').map((op) => op.state)).toEqual(['succeeded']);
  expect(operations.some((op) => op.type === 'workvm.request' || op.type === 'browser.open')).toBe(false);
  const reads = await auditRows(orchestrator, task, 'mail.read');
  expect(reads).toHaveLength(1);
  expect(reads[0]!.result).toBe('ok');
  expect(reads[0]!.detail).toMatchObject({ count: 1, timed_out: false, result: 'ok', folder: 'all' });
  expect(JSON.stringify(reads)).not.toContain('kz74-link');
  await expectNoCredentialLeak(orchestrator, [password]);
  await expectNoPlaintext(orchestrator, CANARIES);
});
