// Start-up check against GET /v1/config (06-web.md section 10.4): a foreign platform root key or origin, or an
// unreachable hub, shows the blocking error page instead of any screen.
import { expect, test } from './fixtures/test.ts';
import { TEST_ROOT_KEY } from './fixtures/env.ts';
import { testConfig } from './fixtures/server.ts';

async function serveConfig(page: import('@playwright/test').Page, body: unknown, status = 200) {
  await page.route('**/v1/config', (route) => route.fulfill({ status, json: body }));
}

test('blocks a hub with another platform root key', async ({ page, baseURL }) => {
  await serveConfig(page, testConfig(baseURL!, 'A'.repeat(43)));
  await page.goto('/nowhere');
  await expect(page.getByTestId('error.title')).toHaveText('Untrusted server');
  await expect(page.getByTestId('error.code')).toHaveText('Error code: platform_root_mismatch');
  await expect(page.getByTestId('notfound.title')).toHaveCount(0);
});

test('blocks a configuration for another origin', async ({ page }) => {
  await serveConfig(page, testConfig('https://agent.example.org', TEST_ROOT_KEY));
  await page.goto('/nowhere');
  await expect(page.getByTestId('error.code')).toHaveText('Error code: origin_mismatch');
});

test('reports an unavailable hub and reloads on request', async ({ page }) => {
  await serveConfig(page, { error: 'unavailable' }, 503);
  await page.goto('/nowhere');
  await expect(page.getByTestId('error.title')).toHaveText('Service unavailable');
  await page.unroute('**/v1/config');
  await page.getByTestId('error.reload').click();
  await expect(page.getByTestId('notfound.title')).toBeVisible();
});
