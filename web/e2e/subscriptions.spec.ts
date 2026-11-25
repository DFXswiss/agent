// "My subscriptions" (KOR-68, ADR-091, rule E-30) against the real hub: a new user sees the card on the home page with
// the way to add a subscription and reaches the page; with readings of the hub (routes fulfilled in the browser) the
// provider line, its windows and accounts, "detailed", "sort by used" and the table show the measured values – on the
// desktop and on both phones (layout, touch targets and accessibility as every signed-in route). The pictures of the
// card and the page are kept in the test output (comparison with the local dashboard's page "Subscriptions").
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { layoutProblems } from './helpers/layout-checks.ts';
import { onboard } from './helpers/onboard.ts';

const DAY = 86_400_000;
const at = (offset: number) => new Date(Date.now() + offset).toISOString();
const reading = (week: number, five: number, model: number) => ({
  fetched_at: at(-4 * 60_000),
  device_id: '01a2b3c4-0000-7000-8000-000000000001',
  unreadable: false,
  windows: [
    { window: 'five_hour', used_percent: five, resets_at: at(3 * 3_600_000 + 12 * 60_000), severity: null },
    { window: 'seven_day', used_percent: week, resets_at: at(6 * DAY), severity: null },
    { window: 'weekly:opus', used_percent: model, resets_at: null, severity: null },
  ],
});
const item = (id: string, primary: boolean, found: unknown) => ({
  id,
  owner_id: '01a2b3c4-0000-7000-8000-0000000000aa',
  primary,
  version: 1,
  epoch: 1,
  created_at: at(-2 * DAY),
  updated_at: at(-DAY),
  reading: found,
});
const LIST = {
  subscriptions: [
    item('01a2b3c4-0000-7000-8000-000000000011', true, reading(14, 41, 21)),
    item('01a2b3c4-0000-7000-8000-000000000012', false, reading(82, 12, 30)),
    item('01a2b3c4-0000-7000-8000-000000000013', false, null),
  ],
};

async function withReadings(page: Page): Promise<void> {
  await page.route('**/v1/me/subscriptions', (route) => route.fulfill({ json: LIST }));
  // records this browser cannot open still show their readings ("cannot be opened")
  await page.route('**/v1/subscriptions/*', (route) => route.fulfill({ json: { record: null } }));
}

test('a new user finds the card on the home page and the way to add a subscription', async ({ page, baseURL }) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  const card = page.getByTestId('subscriptions.card');
  await expect(card).toBeVisible();
  await expect(page.getByTestId('subscriptions.card.empty')).toContainText('No subscription yet');
  await page.getByTestId('subscriptions.card.add').click();
  await expect(page.getByTestId('subscriptions.page')).toBeVisible();
  await expect(page.getByTestId('subscriptions.empty')).toBeVisible();
  await page.getByTestId('subscriptions.add').click();
  await expect(page.getByTestId('subscriptions.add-hint')).toContainText('agent-platform subscriptions add');
});

test('the readings show per provider, opened, detailed, sorted and in the table', async ({ page, baseURL }, info) => {
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await withReadings(page);
  await page.reload();
  await page.getByTestId('login.submit').click();
  const card = page.getByTestId('subscriptions.card');
  await expect(card.getByTestId('subscriptions.provider.used')).toHaveText('82%');
  await expect(card.getByTestId('subscriptions.provider.bar')).toHaveAttribute('data-level', 'warn');
  await card.getByTestId('subscriptions.provider.summary').click();
  await expect(card.getByTestId('subscriptions.window.five_hour')).toContainText('41%');
  await expect(card.getByTestId('subscriptions.account')).toHaveCount(3);
  await expect(card.getByTestId('subscriptions.provider.recorded')).toContainText('4 min ago');
  await card.screenshot({ path: info.outputPath('card.png') });
  await page.getByTestId('subscriptions.card.all').click();
  await expect(page.getByTestId('subscriptions.page')).toBeVisible();
  await expect(page.getByTestId('subscriptions.row')).toHaveCount(3);
  await page.getByTestId('subscriptions.detailed').check();
  await expect(page.getByTestId('subscriptions.provider.details')).toBeVisible();
  await page.getByTestId('subscriptions.sort').check();
  await expect(page.getByTestId('subscriptions.row').first().getByTestId('subscriptions.row.used')).toContainText(
    'Week: 14%',
  );
  await page.screenshot({ path: info.outputPath('page.png'), fullPage: true });
  await page.getByTestId('subscriptions.detailed').uncheck();
  await expect(page.getByTestId('subscriptions.provider.details')).toHaveCount(0);
  expect(await layoutProblems(page)).toEqual([]);
});
