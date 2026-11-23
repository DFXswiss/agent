// The usage strip of a task (06-web.md section 5.2, ADR-010) against the real hub and the device double: the device's
// `usage` outbox items (scripts/hub_test_metrics.py) reach the chat live through the `session` frame, tokens and cost
// add up over the sessions of the task with the basis label of each, the context share is the main session's, and a
// reload shows the same totals from `GET /v1/tasks/{task}`. Also on a phone.
import { devices } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { seedUsage } from '../helpers/monitoring.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

test('shows tokens, cost with its basis and the context share live and after a reload @R-6.1', async ({
  page,
  baseURL,
}) => {
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Count the tokens');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('usage.none')).toBeVisible();
  await seedUsage(user.device, main.session_id, {
    input_tokens: 12_000,
    output_tokens: 800,
    cache_read_tokens: 400,
    cost_micro_usd: 1300,
    cost_basis: 'provider',
    context_used_tokens: 40_000,
    context_window_tokens: 160_000,
  });
  await expect(page.getByTestId('usage.tokens')).toHaveText('12,000 in, 800 out, 400 cache');
  await expect(page.getByTestId('usage.cost')).toHaveText('$0.0013 (provider)');
  await expect(page.getByTestId('usage.context')).toHaveText('25%');
  // a second item adds to the counter
  await seedUsage(user.device, main.session_id, {
    input_tokens: 1000,
    output_tokens: 200,
    cost_micro_usd: 2_000_000,
    cost_basis: 'provider',
    context_used_tokens: 80_000,
    context_window_tokens: 160_000,
  });
  await expect(page.getByTestId('usage.cost')).toHaveText('$2.00 (provider)');
  await expect(page.getByTestId('usage.context')).toHaveText('50%');
  await page.reload();
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('usage.tokens')).toHaveText('13,000 in, 1,000 out, 400 cache');
  await expect(page.getByTestId('usage.context')).toHaveText('50%');
});

// the phone profile without its browser type (a describe group cannot switch the worker's browser)
const { defaultBrowserType: _browser, ...PHONE } = devices['Pixel 7'];

test.describe('on a phone', () => {
  test.use(PHONE);

  test('the strip wraps without horizontal scrolling @R-6.1', async ({ page, baseURL }) => {
    const user = await freshUser(page, baseURL!, { poolReady: 2 });
    const task = await sendPrompt(page, 'Usage on a phone');
    const main = await mainSession(task);
    await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
    await seedUsage(user.device, main.session_id, { cost_basis: 'list_price', context_window_tokens: 0 });
    await expect(page.getByTestId('usage.cost')).toContainText('(list price)');
    await expect(page.getByTestId('usage.context')).toHaveText('unknown');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
});
