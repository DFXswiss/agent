// The automatable rows of the PRF device matrix (06-web.md section 12.7, ADR-006, WP-M3-06; results in
// docs/platform/manual-checks/): Chromium on the desktop and with the Pixel 7 and iPhone 15 emulation, each with the
// CDP virtual authenticator as platform passkey with PRF, as platform passkey without PRF, and as a roaming security
// key (`transport: "usb"`) with PRF. Per row one test walks the matrix columns against the real hub with the device
// double: registration, login, PRF (unlock without the recovery code, or the `prf_missing` path), recovery after losing
// the passkey, countdown to "working" and the read-only terminal. Real platforms (iCloud Keychain, Google Password
// Manager, Windows Hello, password managers, hardware keys) need a human; their rows stay open in the checklist.
import { devices, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { addAuthenticator, type AuthenticatorOptions, type VirtualAuthenticator } from '../helpers/authenticator.ts';
import { mainSession, newDevice, walk } from '../helpers/director.ts';
import { onboard, type Onboarded } from '../helpers/onboard.ts';
import { sessionEvents, terminalChunk } from '../helpers/sessions.ts';
import { sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { navigateInApp } from '../helpers/whiteboard.ts';

/** A device profile without its browser type (a describe group cannot switch it; the matrix runs on Chromium, the
 *  iPhone profile as well, like the phone projects of playwright.config.ts). */
function emulate(name: string) {
  const { defaultBrowserType: _browser, ...use } = devices[name]!;
  return use;
}
const PROFILES = [
  { name: 'desktop', use: {} },
  { name: 'pixel-7', use: emulate('Pixel 7') },
  { name: 'iphone-15', use: emulate('iPhone 15') },
] as const;
const AUTHENTICATORS: readonly { name: string; options: Partial<AuthenticatorOptions> }[] = [
  { name: 'platform passkey with PRF', options: {} },
  { name: 'platform passkey without PRF', options: { hasPrf: false } },
  { name: 'security key (usb) with PRF', options: { transport: 'usb' } },
];

async function signOut(page: Page): Promise<void> {
  await page.getByTestId('shell.settings').click();
  await page.getByTestId('settings.sign-out').click();
  await expect(page).toHaveURL(/\/login$/);
}

/** Login after sign-out: with PRF the passkey alone unlocks; without PRF the recovery code does (`prf_missing`). */
async function login(page: Page, done: Onboarded, prf: boolean): Promise<void> {
  await page.getByTestId('login.submit').click();
  if (!prf) {
    await expect(page.getByTestId('prf-missing.explain')).toBeVisible();
    await page.getByTestId('prf-missing.recovery.input').fill(done.recoveryCode);
    await page.getByTestId('prf-missing.recovery.submit').click();
  }
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
}

/** Recovery after losing every passkey: the code registers a replacement passkey and issues a new code; the old code
 *  is refused afterwards. */
async function recover(
  page: Page,
  lost: VirtualAuthenticator,
  done: Onboarded,
  options: Partial<AuthenticatorOptions>,
) {
  await signOut(page);
  await lost.cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: lost.id });
  const replacement = await addAuthenticator(page, options);
  await page.getByTestId('login.recover').click();
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await page.getByTestId('recover.create-passkey').click();
  await page.getByTestId('recover.unlock').click();
  const fresh = (await page.getByTestId('recovery-code.code').textContent())!;
  expect(fresh).not.toBe(done.recoveryCode);
  await page.getByTestId('recovery-code.confirm').fill(fresh.slice(-6));
  await page.getByTestId('recovery-code.continue').click();
  if (options.hasPrf === false) await page.getByTestId('prf-missing.offer.skip').click();
  await expect(page).toHaveURL(new RegExp(`/o/${done.org}$`));
  expect(await replacement.credentials()).toHaveLength(1);
  // the old recovery code is gone
  await signOut(page);
  await page.goto('/recover');
  await page.getByTestId('recover.code').fill(done.recoveryCode);
  await page.getByTestId('recover.submit').click();
  await expect(page.getByTestId('recover.notice')).toHaveText(
    'No account was found for this recovery code. Check the code.',
  );
}

/** First task from the org home (signed in): the countdown runs through the start steps to "Ready in", then the
 *  terminal of the session shows a chunk (in-app navigation: a reload would need the unlock again). */
async function countdownAndTerminal(page: Page): Promise<void> {
  const task = await sendPrompt(page, 'What is 2+2?');
  await expect(page.getByTestId('countdown.digits')).toBeVisible();
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 50 });
  await expect(page.getByTestId('countdown.summary')).toHaveText(/^Ready in \d+\.\d s$/);
  await sessionEvents(session.session_id, [terminalChunk([[0, 'matrix row']])]);
  await navigateInApp(page, `${new URL(page.url()).pathname.replace(/\/t\/.*$/, '')}/s/${session.session_id}`);
  await expect(page.getByTestId('terminal.label')).toHaveText('Read-only view – prompts are entered in the chat');
  await expect(page.getByTestId('terminal')).toHaveAttribute('data-chunks', '1');
  await expect(page.getByTestId('terminal.screen.a')).toHaveAttribute('data-chars', String('matrix row'.length));
}

for (const profile of PROFILES) {
  test.describe(`PRF matrix on ${profile.name}`, () => {
    test.use(profile.use);
    for (const authenticator of AUTHENTICATORS) {
      const prf = authenticator.options.hasPrf !== false;
      test(`${authenticator.name}: registration, login, PRF, recovery, countdown, terminal`, async ({
        page,
        baseURL,
      }) => {
        test.setTimeout(120_000);
        const first = await addAuthenticator(page, authenticator.options);
        await newDevice({ poolReady: 2 });
        // registration
        const done = await onboard(page, { origin: baseURL! });
        expect(await first.credentials()).toHaveLength(1);
        // PRF: the passkey list records whether the credential delivered PRF output
        await page.getByTestId('shell.settings').click();
        await expect(page.getByTestId('passkeys.item')).toHaveAttribute('data-prf', String(prf));
        // login
        await signOut(page);
        await login(page, done, prf);
        // countdown and terminal
        await countdownAndTerminal(page);
        // recovery
        await recover(page, first, done, authenticator.options);
      });
    }
  });
}
