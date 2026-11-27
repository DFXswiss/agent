// "My subscriptions" with readings (KOR-68, ADR-091, rule E-30) against the real hub and the device double: the device
// logs the user's subscription in (relay, it stores the record) and reports its windows; the card on the home page
// and the page show what is left: the provider line, its windows and the account with its address, the source of its
// reading and all its windows (the binding one marked), "detailed", "least free first" and the table – on the desktop and on both phones (layout, touch targets and accessibility). The pictures of the card
// and the page are kept in the test output (comparison with the local dashboard's page "Subscriptions").
import { expect, test } from '../fixtures/test.ts';
import { answer, reportQuotas } from '../helpers/credentials.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

test('the readings show per provider, opened, detailed, sorted and in the table', async ({ page, baseURL }, info) => {
  const user = await freshUser(page, baseURL!);
  const api = await PageUser.open(page, user.recoveryCode);
  await page.goto('/settings');
  await page.getByTestId('login.submit').click();
  await page.getByTestId('claude.connect').click();
  await expect(page.getByTestId('claude.devices.select')).toHaveValue(user.device);
  await page.getByTestId('claude.start').click();
  await expect(page.getByTestId('claude.waiting')).toBeVisible();
  await answer(user.device, [{ body: { step: 'authorize', url: 'https://claude.ai/oauth/authorize?code=true' } }]);
  await expect(page.getByTestId('claude.authorize.open')).toBeVisible();
  await page.getByTestId('claude.code').fill('the-code#state');
  await page.getByTestId('claude.code.send').click();
  await expect(page.getByTestId('claude.code.send')).toHaveText('Code sent');
  await answer(user.device, [
    {
      body: { step: 'done', email: 'seat-1@example.com' },
      final: true,
      store: { kind: 'claude_subscription', email: 'seat-1@example.com' },
    },
  ]);
  await expect(page.getByTestId('claude.connected')).toBeVisible();
  await reportQuotas(user.device, api.userId, [
    { window: 'five_hour', used_percent: 41, resets_in_s: 3 * 3600 + 12 * 60 },
    { window: 'seven_day', used_percent: 82, resets_in_s: 6 * 86400 },
    { window: 'weekly:opus', used_percent: 21 },
  ]);
  await page.goto(`/o/${user.org}`);
  await page.getByTestId('login.submit').click();
  const card = page.getByTestId('subscriptions.card');
  // the provider line is what is left of the total of its accounts; with one account that is its own week
  await expect(card.getByTestId('subscriptions.provider.total')).toHaveText('1 account');
  await expect(card.getByTestId('subscriptions.provider.usable')).toHaveText('usable now: 1 of 1');
  await expect(card.getByTestId('subscriptions.provider.free')).toHaveText('18% free');
  await expect(card.getByTestId('subscriptions.provider.bar')).toHaveAttribute('data-level', 'warn');
  await expect(card.getByTestId('subscriptions.provider.release')).toHaveCount(0);
  await card.getByTestId('subscriptions.provider.summary').click();
  await expect(card.getByTestId('subscriptions.window.five_hour')).toContainText('59% free');
  await expect(card.getByTestId('subscriptions.window.weekly:opus')).toContainText('79% free');
  const account = card.getByTestId('subscriptions.account');
  await expect(account).toContainText('seat-1@example.com');
  await expect(account.getByTestId('subscriptions.account.age')).toContainText('platform device');
  // every window of the subscription, the week (18 % left against 59 %) marked as the binding one
  const windows = account.getByTestId('subscriptions.account.window');
  await expect(windows).toHaveCount(3);
  await expect(windows.nth(0)).toHaveAttribute('data-binding', 'no');
  await expect(windows.nth(0)).toContainText('59% free');
  await expect(windows.nth(0)).toContainText('3 h ');
  await expect(windows.nth(1)).toHaveAttribute('data-binding', 'yes');
  await expect(windows.nth(1)).toContainText('18% free');
  await expect(windows.nth(2)).toContainText('79% free');
  await expect(account.getByTestId('subscriptions.account.blocked')).toHaveCount(0);
  await expect(card.getByTestId('subscriptions.provider.recorded')).toBeVisible();
  await card.screenshot({ path: info.outputPath('card.png') });
  await card.getByTestId('subscriptions.card.all').click();
  await expect(page.getByTestId('subscriptions.page')).toBeVisible();
  const row = page.getByTestId('subscriptions.row');
  await expect(row).toHaveCount(1);
  await expect(row.getByTestId('subscriptions.row.status')).toHaveText('logged in');
  await expect(row.getByTestId('subscriptions.row.free')).toContainText('Week: 18%');
  await expect(row.getByTestId('subscriptions.row.free')).toContainText('5h: 59%');
  await page.getByTestId('subscriptions.detailed').check();
  await expect(page.getByTestId('subscriptions.provider.details')).toBeVisible();
  await page.getByTestId('subscriptions.sort').check();
  await page.screenshot({ path: info.outputPath('page.png'), fullPage: true });
  await page.getByTestId('subscriptions.detailed').uncheck();
  await expect(page.getByTestId('subscriptions.provider.details')).toHaveCount(0);
  expect(await layoutProblems(page)).toEqual([]);
  // the 5 hours used up while the week is nearly free: not usable now, until the 5 hours reset
  await reportQuotas(user.device, api.userId, [
    { window: 'five_hour', used_percent: 100, resets_in_s: 90 * 60 },
    { window: 'seven_day', used_percent: 13, resets_in_s: 4 * 86400 },
  ]);
  await page.goto('/subscriptions');
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('subscriptions.provider.usable')).toHaveText('usable now: 0 of 1');
  await expect(page.getByTestId('subscriptions.provider.release')).toContainText('next release');
  // the total counts the subscription with what its binding window has left: the 5 hours are used up, so 0 %
  await expect(page.getByTestId('subscriptions.provider.free')).toHaveText('0% free');
  await page.getByTestId('subscriptions.detailed').check();
  const used = page.getByTestId('subscriptions.account');
  await expect(used).toHaveAttribute('data-exhausted', 'yes');
  await expect(used.getByTestId('subscriptions.account.blocked')).toContainText('not usable now until');
  const five = used.getByTestId('subscriptions.account.window').first();
  await expect(five).toHaveAttribute('data-binding', 'yes');
  await expect(five.getByTestId('subscriptions.account.free')).toHaveText('0% free');
  await expect(five.getByTestId('subscriptions.account.bar')).toHaveAttribute('data-level', 'low');
  await expect(page.getByTestId('subscriptions.row.free')).toContainText('5h: 0%');
  await page.screenshot({ path: info.outputPath('page-used-up.png'), fullPage: true });
  expect(await layoutProblems(page)).toEqual([]);
});
