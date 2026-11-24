// Not-found screen under the strict hub CSP (WEB-01 acceptance): renders, links home, no CSP violation.
import { CACHE_INDEX, CONTENT_SECURITY_POLICY } from './fixtures/headers.ts';
import { expect, test } from './fixtures/test.ts';

test('renders the not-found page under the strict CSP', async ({ page }) => {
  const response = await page.goto('/no/such/page');
  expect(response?.headers()['content-security-policy']).toBe(CONTENT_SECURITY_POLICY);
  expect(response?.headers()['cache-control']).toBe(CACHE_INDEX);
  await expect(page.getByTestId('notfound.title')).toHaveText('Page not found');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  // the start page sends an anonymous visitor to the sign-in (06-web.md section 2.4)
  await page.getByTestId('notfound.home').click();
  await expect(page).toHaveURL('/login');
  await expect(page.getByTestId('login.page')).toBeVisible();
});

test('follows the browser language', async ({ newContext }) => {
  const context = await newContext({ locale: 'de-CH' });
  const page = await context.newPage();
  await page.goto('/x');
  await expect(page.getByTestId('notfound.title')).toHaveText('Seite nicht gefunden');
  await expect(page.locator('html')).toHaveAttribute('lang', 'de');
});

test('serves hashed assets as immutable with integrity', async ({ page, request }) => {
  await page.goto('/');
  // the first load is several module scripts (framework, start, runtime; 06-web.md section 12.2): each one is checked
  const scripts = await page.locator('script[type="module"]').all();
  expect(scripts.length).toBeGreaterThan(0);
  for (const script of scripts) {
    await expect(script).toHaveAttribute('integrity', /^sha384-/);
    await expect(script).toHaveAttribute('crossorigin', 'anonymous');
    const asset = await request.get((await script.getAttribute('src'))!);
    expect(asset.headers()['cache-control']).toMatch(/\bimmutable\b/);
    expect(asset.headers()['cache-control']).toMatch(/\bmax-age=31536000\b/);
    expect(asset.headers()['content-security-policy']).toBe(CONTENT_SECURITY_POLICY);
  }
  expect((await request.get('/assets/missing.js')).status()).toBe(404);
});
