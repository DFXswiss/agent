// The settings section "Mailboxes" (KOR-74, ADR-099 section 9) against the real hub and the device double: add a
// mailbox (the first one is the mailbox of the automatic login), a second one with a taken name refused, release it to
// the user's workgroup (sealed in the record and mirrored at the hub), check it on a device (the device opens the
// mailbox's body), change it with the password kept, and delete it after one confirmation – on the desktop and on a
// phone. No password ever reaches the DOM.
import { devices, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { answer } from '../helpers/credentials.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

const PASSWORD = 'not a real mailbox password';

async function mailboxes(page: Page): Promise<void> {
  await page.goto('/settings');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('mailboxes.section')).toBeVisible();
  await expect(page.getByTestId('mailboxes.empty')).toBeVisible();
}

async function add(page: Page, name: string): Promise<void> {
  await page.getByTestId('mailboxes.add').click();
  const form = page.getByTestId('mbform');
  await form.getByTestId('mbform.name').fill(name);
  await form.getByTestId('mbform.purpose').fill('sign-in mails of new subscriptions');
  await form.getByTestId('mbform.email').fill('claude@example.com');
  await form.getByTestId('mbform.imap_host').fill('imap.example.com');
  await expect(form.getByTestId('mbform.imap_port')).toHaveValue('993');
  await form.getByTestId('mbform.imap_user').fill('claude@example.com');
  await form.getByTestId('mbform.imap_password').fill(PASSWORD);
}

async function expectNoPassword(page: Page): Promise<void> {
  expect(await page.content()).not.toContain(PASSWORD);
}

test('adds, releases, checks, changes and deletes a mailbox', async ({ page, baseURL }) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await mailboxes(page);
  await add(page, 'signin');
  await page.getByTestId('mbform.save').click();
  const card = page.locator('[data-testid^="mbcard."][data-name="signin"]');
  await expect(card.getByTestId('mbcard.primary')).toHaveText('for the automatic login');
  await expect(card.getByTestId('mbcard.email')).toHaveText('claude@example.com');
  await expect(card.getByTestId('mbcard.purpose')).toHaveText('sign-in mails of new subscriptions');
  await expect(card.getByTestId('mbcard.released')).toHaveText('Released to no workgroup.');
  await expectNoPassword(page);
  // a second mailbox of the same name is refused before it is sent
  await add(page, 'signin');
  await expect(page.getByTestId('mbform.name.taken')).toBeVisible();
  await expect(page.getByTestId('mbform.save')).toBeDisabled();
  await page.getByTestId('mbform.cancel').click();
  // release to the user's workgroup
  await card.getByTestId('mbcard.release').click();
  const choice = card.locator('[data-testid^="mbrelease.wg."]');
  await expect(choice).toHaveCount(1);
  const wg = (await choice.getAttribute('data-testid'))!.slice('mbrelease.wg.'.length);
  await choice.check();
  await card.getByTestId('mbrelease.save').click();
  await expect(card.getByTestId('mbrelease.saved')).toBeVisible();
  await expect(card.getByTestId(`mbcard.released.${wg}`)).toBeVisible();
  const listed = (await api.request('GET', '/v1/me/mailboxes')) as {
    mailboxes: { id: string; primary: boolean; use: { wgs: string[] } | null }[];
  };
  expect(listed.mailboxes).toEqual([expect.objectContaining({ primary: true, use: { wgs: [wg] } })]);
  await card.getByTestId('mbcard.release.close').click();
  // check it on the device: the device opens the mailbox's body
  await card.getByTestId('mbcard.check').click();
  await expect(card.getByTestId('mbcheck.devices.select')).toHaveValue(user.device);
  await card.getByTestId('mbcheck.start').click();
  await expect(card.getByTestId('mbcheck.waiting')).toBeVisible();
  const opened = await answer(user.device, [{ body: { step: 'done', valid: true }, final: true }]);
  expect(opened).toMatchObject({
    kind: 'mailbox.check',
    body: { mailbox: { name: 'signin', imap_password: PASSWORD, use: { wgs: [wg] } } },
  });
  await expect(card.getByTestId('mbcheck.result')).toHaveText('The device signed in to the mailbox.');
  await card.getByTestId('mbcheck.close').click();
  // change it: the password field stays empty and the stored password stays
  await card.getByTestId('mbcard.edit').click();
  await expect(card.getByTestId('mbform.imap_password')).toHaveValue('');
  await card.getByTestId('mbform.purpose').fill('the login of the team subscription');
  await card.getByTestId('mbform.save').click();
  await expect(card.getByTestId('mbcard.purpose')).toHaveText('the login of the team subscription');
  await card.getByTestId('mbcard.check').click();
  await card.getByTestId('mbcheck.start').click();
  await expect(card.getByTestId('mbcheck.waiting')).toBeVisible();
  expect(await answer(user.device, [{ body: { step: 'done', valid: false }, final: true }])).toMatchObject({
    body: { mailbox: { imap_password: PASSWORD, purpose: 'the login of the team subscription' } },
  });
  await expect(card.getByTestId('mbcheck.result')).toHaveText('The device could not sign in to the mailbox.');
  await card.getByTestId('mbcheck.close').click();
  await expectNoPassword(page);
  // delete it after one confirmation
  await card.getByTestId('mbcard.remove').click();
  await card.getByTestId('mbcard.remove.no').click();
  await card.getByTestId('mbcard.remove').click();
  await card.getByTestId('mbcard.remove.yes').click();
  await expect(page.getByTestId('mailboxes.empty')).toBeVisible();
  expect(await api.request('GET', '/v1/me/mailboxes')).toEqual({ mailboxes: [] });
});

// the phone profile without its browser type (a describe group cannot switch the worker's browser)
const { defaultBrowserType: _browser, ...PHONE } = devices['Pixel 7'];

test.describe('on a phone', () => {
  test.use(PHONE);

  test('the section fits the screen and keeps touch targets', async ({ page, baseURL }) => {
    await freshUser(page, baseURL!);
    await mailboxes(page);
    await add(page, 'phone');
    await page.getByTestId('mbform.save').click();
    const card = page.locator('[data-testid^="mbcard."][data-name="phone"]');
    await expect(card.getByTestId('mbcard.primary')).toBeVisible();
    await card.getByTestId('mbcard.release').click();
    await expect(card.getByTestId('mbrelease')).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    for (const id of ['mailboxes.add', 'mbrelease.save', 'mbcard.release.close']) {
      const box = (await page.getByTestId(id).boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
    await expectNoPassword(page);
  });
});
