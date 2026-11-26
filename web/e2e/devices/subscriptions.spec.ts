// "My subscriptions" with readings (KOR-68, ADR-091, rule E-30) against the real hub and the device double: the device
// logs the user's subscription in (relay, it stores the record) and reports its windows; the card on the home page
// and the page show the provider line, its windows and the account with its address, "detailed", "sort by used" and
// the table – on the desktop and on both phones (layout, touch targets and accessibility). The pictures of the card
// and the page are kept in the test output (comparison with the local dashboard's page "Subscriptions"). "Who used how
// much" (KOR-75, ADR-100): the device reports a session's tokens on that subscription; the page shows the person, the
// tokens and the estimated share of the measured 5-hour window, then the week and a chosen span.
import { expect, test } from '../fixtures/test.ts';
import { answer, reportQuotas } from '../helpers/credentials.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { layoutProblems } from '../helpers/layout-checks.ts';
import { seedUsage } from '../helpers/monitoring.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

test('who used how much: the person, the tokens and the estimated share per period', async ({
  page,
  baseURL,
}, info) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
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
      body: { step: 'done', email: 'seat-2@example.com' },
      final: true,
      store: { kind: 'claude_subscription', email: 'seat-2@example.com' },
    },
  ]);
  await expect(page.getByTestId('claude.connected')).toBeVisible();
  await reportQuotas(user.device, api.userId, [
    { window: 'five_hour', used_percent: 40, resets_in_s: 2 * 3600 },
    { window: 'seven_day', used_percent: 10, resets_in_s: 5 * 86400 },
  ]);
  const listed = (await api.request('GET', '/v1/me/subscriptions')) as { subscriptions: { id: string }[] };
  const subscription = listed.subscriptions[0]!.id;
  await page.goto(`/o/${user.org}`);
  await page.getByTestId('login.submit').click();
  const task = await sendPrompt(page, 'Use the subscription');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await seedUsage(user.device, main.session_id, {
    input_tokens: 12_000,
    output_tokens: 800,
    cache_read_tokens: 4_000,
    cache_write_tokens: 200,
    subscription_id: subscription,
    triggered_by: api.userId,
  });
  await page.goto('/subscriptions');
  await page.getByTestId('login.submit').click();
  const section = page.getByTestId('subusage.section');
  await expect(section).toBeVisible();
  const block = section.getByTestId('subusage.subscription');
  await expect(block).toHaveAttribute('data-key', subscription);
  await expect(block.getByTestId('subusage.measured')).toContainText('40%');
  const person = block.getByTestId('subusage.person');
  await expect(person).toHaveAttribute('data-user', api.userId);
  await expect(person).toContainText('you');
  await expect(person.getByTestId('subusage.tokens')).toHaveText('17K');
  // the only person used all tokens: the estimated share is the whole measured use of the window
  await expect(person.getByTestId('subusage.estimated')).toHaveText('≈ 40%');
  await expect(section.getByTestId('subusage.estimate-note')).toContainText('estimate');
  await expect(section.getByTestId('subusage.dfx.link')).toHaveAttribute('href', '/settings#billing');
  await page.screenshot({ path: info.outputPath('usage.png'), fullPage: true });
  expect(await layoutProblems(page)).toEqual([]);
  await section.getByTestId('subusage.period.week').check();
  await expect(block.getByTestId('subusage.estimated')).toHaveText('≈ 10%');
  await section.getByTestId('subusage.period.range').check();
  await expect(section.getByTestId('subusage.estimate-note')).toContainText('measures no quota');
  await expect(block.getByTestId('subusage.estimated')).toHaveText('—');
  await expect(person.getByTestId('subusage.tokens')).toHaveText('17K');
  expect(await layoutProblems(page)).toEqual([]);
});

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
  // the provider line is the total of its accounts; with one account that is its own week
  await expect(card.getByTestId('subscriptions.provider.total')).toHaveText('1 account');
  await expect(card.getByTestId('subscriptions.provider.used')).toHaveText('82%');
  await expect(card.getByTestId('subscriptions.provider.bar')).toHaveAttribute('data-level', 'warn');
  await card.getByTestId('subscriptions.provider.summary').click();
  await expect(card.getByTestId('subscriptions.window.five_hour')).toContainText('41%');
  await expect(card.getByTestId('subscriptions.window.weekly:opus')).toContainText('21%');
  await expect(card.getByTestId('subscriptions.account')).toContainText('seat-1@example.com');
  await expect(card.getByTestId('subscriptions.provider.recorded')).toBeVisible();
  await card.screenshot({ path: info.outputPath('card.png') });
  await card.getByTestId('subscriptions.card.all').click();
  await expect(page.getByTestId('subscriptions.page')).toBeVisible();
  const row = page.getByTestId('subscriptions.row');
  await expect(row).toHaveCount(1);
  await expect(row.getByTestId('subscriptions.row.status')).toHaveText('logged in');
  await expect(row.getByTestId('subscriptions.row.used')).toContainText('Week: 82%');
  await page.getByTestId('subscriptions.detailed').check();
  await expect(page.getByTestId('subscriptions.provider.details')).toBeVisible();
  await page.getByTestId('subscriptions.sort').check();
  await page.screenshot({ path: info.outputPath('page.png'), fullPage: true });
  await page.getByTestId('subscriptions.detailed').uncheck();
  await expect(page.getByTestId('subscriptions.provider.details')).toHaveCount(0);
  expect(await layoutProblems(page)).toEqual([]);
});
