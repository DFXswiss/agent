// "My subscriptions" (KOR-68, ADR-091, rule E-30) against the real hub: a new user sees the card on the home page with
// the way to add a subscription and reaches the page, on the desktop and on both phones. The readings of a subscription
// are in `e2e/devices/subscriptions.spec.ts` (the device double logs it in and measures it).
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { onboard } from './helpers/onboard.ts';

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
  await expect(page.getByTestId('subadd.name')).toBeVisible();
});
