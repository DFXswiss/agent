// Subscriptions logged in side by side (KOR-73, ADR-100) against the real hub and the device double, which announces
// `subscriptions.parallel`: on the page "My subscriptions" the owner adds two subscriptions at once – one with its own
// mailbox and the data of a new account (automatic), one with link and code – for the organization; each is written
// first (no token, the use mirrored at the hub) and logged in by its own request with its own slot. The progress of each
// shows in its row; the automatic one meets a check for humans: the picture shows, the owner's click reaches the device
// as a follow-up, the check passes and the login ends; the other one is given up to the relay ("sign in myself"), names
// the reason and ends with the pasted code. No mailbox password reaches the page.
import { expect, test } from '../fixtures/test.ts';
import { answer } from '../helpers/credentials.ts';
import { freshUser } from '../helpers/tasks.ts';

const PASSWORD = 'mailbox-password-of-the-suite';
// a 1×1 JPEG
const JPEG =
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==';

interface LoginBody {
  readonly mode: string;
  readonly subscription_id: string;
  readonly extended?: boolean;
  readonly account?: { readonly name: string; readonly consents?: readonly string[] };
}

test('two subscriptions log in side by side with progress, a check for humans and the relay', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  await page.goto('/subscriptions');
  await page.getByTestId('login.submit').click();
  await page.getByTestId('subscriptions.add-several').click();
  await page.getByTestId('subbatch.entry.0.name').fill('Seat one');
  await page.getByTestId('subbatch.entry.0.email').fill('seat-1@example.com');
  await page.getByTestId('subbatch.entry.0.mailbox.host').fill('imap.example.com');
  await page.getByTestId('subbatch.entry.0.mailbox.user').fill('seat-1@example.com');
  await page.getByTestId('subbatch.entry.0.mailbox.password').fill(PASSWORD);
  await page.getByTestId('subbatch.entry.0.mailbox.email').fill('seat-1@example.com');
  await page.getByTestId('subbatch.entry.0.account.on').check();
  await page.getByTestId('subbatch.entry.0.account.name').fill('Ada Example');
  await expect(page.getByTestId('subbatch.entry.0.account.consent.terms')).not.toBeChecked();
  await page.getByTestId('subbatch.entry.0.account.consent.terms').check();
  await page.getByTestId('subbatch.entry.1.name').fill('Seat two');
  await page.getByTestId(`subbatch.orgs.${user.org}`).check();
  await page.getByTestId('subbatch.start').click();
  const progress = page.getByTestId('subbatch.progress');
  await expect(progress).toBeVisible();

  // both logins are open at the hub at once; the device double answers each by its request
  const first = await answer(user.device, [{ body: { step: 'progress', stage: 'browser_started' } }]);
  const second = await answer(user.device, [{ body: { step: 'progress', stage: 'browser_started' } }], {
    skip: [first.request_id],
  });
  const [auto, manual] = (first.body as LoginBody).mode === 'automatic' ? [first, second] : [second, first];
  const autoBody = auto.body as LoginBody;
  const manualBody = manual.body as LoginBody;
  expect(autoBody).toMatchObject({ extended: true, account: { name: 'Ada Example', consents: ['terms'] } });
  expect(manualBody).toMatchObject({ mode: 'manual', extended: true });
  const autoRow = page.getByTestId(`subbatch.row.${autoBody.subscription_id}`);
  const manualRow = page.getByTestId(`subbatch.row.${manualBody.subscription_id}`);
  await expect(autoRow.getByTestId('claude.progress')).toHaveAttribute('data-stage', 'browser_started');
  await expect(manualRow.getByTestId('claude.progress')).toHaveAttribute('data-stage', 'browser_started');

  // the check for humans: picture, the owner's click as a follow-up, passed, done
  await answer(user.device, [{ body: { step: 'verify', frame: 1, image: { width: 1, height: 1, data: JPEG } } }], {
    request_id: auto.request_id,
  });
  await expect(autoRow.getByTestId('claude.verify')).toHaveAttribute('data-frame', '1');
  const clickPosted = page.waitForResponse(
    (response) => response.url().endsWith('/v1/device-requests') && response.request().method() === 'POST',
  );
  await autoRow.getByTestId('claude.verify.picture').click();
  await clickPosted;
  await expect(autoRow.getByTestId('claude.verify.clicked')).toBeVisible();
  const click = await answer(user.device, [], { skip: [auto.request_id, manual.request_id] });
  expect(click.body).toMatchObject({ click: { x: expect.any(Number), y: expect.any(Number) } });
  await answer(
    user.device,
    [
      { body: { step: 'progress', stage: 'verified' } },
      { body: { step: 'done', email: 'seat-1@example.com' }, final: true },
    ],
    { request_id: auto.request_id },
  );
  await expect(autoRow.getByTestId('subbatch.done')).toBeVisible();

  // the other one: given up to the relay, with the reason, then link and code
  await answer(user.device, [{ body: { step: 'verify', frame: 1, image: { width: 1, height: 1, data: JPEG } } }], {
    request_id: manual.request_id,
  });
  const relayPosted = page.waitForResponse(
    (response) => response.url().endsWith('/v1/device-requests') && response.request().method() === 'POST',
  );
  await manualRow.getByTestId('claude.verify.relay').click();
  await relayPosted;
  const relay = await answer(user.device, [], { skip: [manual.request_id, click.request_id] });
  expect(relay.body).toEqual({ relay: true });
  await answer(
    user.device,
    [
      {
        body: {
          step: 'authorize',
          url: 'https://claude.ai/oauth/authorize?code=true',
          automation: 'failed',
          reason: 'human_check',
        },
      },
    ],
    { request_id: manual.request_id },
  );
  await expect(manualRow.getByTestId('claude.automation-reason')).toHaveAttribute('data-reason', 'human_check');
  await manualRow.getByTestId('claude.code').fill('the-code#state');
  await manualRow.getByTestId('claude.code.send').click();
  await expect(manualRow.getByTestId('claude.code.send')).toHaveText('Code sent');
  // the code is the newest open request now (the relay stays open until the chain's final answer)
  const code = await answer(user.device, [{ body: { step: 'done' }, final: true }]);
  expect(code.body).toEqual({ code: 'the-code#state' });
  await expect(manualRow.getByTestId('subbatch.done')).toBeVisible();
  expect(await page.content()).not.toContain(PASSWORD);
  await page.getByTestId('subbatch.close').click();
  await expect(page.getByTestId('subscriptions.row')).toHaveCount(2);
});

test('one subscription with its own mailbox and a new account is written first and logged in automatically', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!);
  await page.goto('/subscriptions');
  await page.getByTestId('login.submit').click();
  await page.getByTestId('subscriptions.add').click();
  await page.getByTestId('subadd.name').fill('Seat three');
  await page.getByTestId('subadd.note').fill('for the reviews');
  await page.getByTestId('subadd.email').fill('seat-3@example.com');
  await page.getByTestId('subadd.mailbox.host').fill('imap.example.com');
  await page.getByTestId('subadd.mailbox.user').fill('seat-3@example.com');
  await page.getByTestId('subadd.mailbox.password').fill(PASSWORD);
  await expect(page.getByTestId('subadd.start')).toBeDisabled(); // the mailbox is not complete yet
  await page.getByTestId('subadd.mailbox.email').fill('seat-3@example.com');
  await page.getByTestId('subadd.account.on').check();
  await expect(page.getByTestId('subadd.start')).toBeDisabled(); // the account has no name yet
  await page.getByTestId('subadd.account.name').fill('Grace Example');
  await page.getByTestId('subadd.account.birth').fill('1990-04-01');
  await page.getByTestId('subadd.account.consent.terms').check();
  await page.getByTestId('subadd.account.consent.age').check();
  await page.getByTestId(`subadd.orgs.${user.org}`).check();
  const posted = page.waitForResponse(
    (response) => response.url().endsWith('/v1/device-requests') && response.request().method() === 'POST',
  );
  await page.getByTestId('subadd.start').click();
  await posted;
  const started = await answer(user.device, [{ body: { step: 'progress', stage: 'registered' } }]);
  expect(started.body).toMatchObject({
    mode: 'automatic',
    extended: true,
    email: 'seat-3@example.com',
    account: { name: 'Grace Example', birth_date: '1990-04-01', consents: ['terms', 'age'] },
  });
  await expect(page.getByTestId('claude.progress')).toHaveAttribute('data-stage', 'registered');
  await answer(user.device, [{ body: { step: 'done', email: 'seat-3@example.com' }, final: true }], {
    request_id: started.request_id,
  });
  await expect(page.getByTestId('subscriptions.row')).toHaveCount(1);
  expect(await page.content()).not.toContain(PASSWORD);
});
