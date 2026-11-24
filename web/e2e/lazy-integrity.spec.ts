// KOR-04 (AUD-01 A-08; 04-crypto.md section 9, 06-web.md section 10.2): a lazily loaded chunk carries integrity like
// the start chunks. The production build served by the hub; the browser fetches the chunk of the lazily loaded screens
// (with the security screen, HOT-16), the test changes its bytes on the way, and the browser refuses it: the changed
// code never runs and the screen stays away.
import { expect, test } from './fixtures/test.ts';

const CHUNK = /\/assets\/screens-[\w-]+\.js$/;

test('a lazily loaded chunk whose bytes were changed is refused and does not run', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  const served: string[] = [];
  await page.route(CHUNK, async (route) => {
    const response = await route.fetch();
    served.push(route.request().url());
    const body = `${await response.text()}\ndocument.documentElement.dataset.tampered = 'ran';\n`;
    await route.fulfill({ response, body });
  });
  await page.goto('/login');
  await page.getByTestId('shell.footer.security').click();
  await expect.poll(() => served.length).toBeGreaterThan(0);
  await expect.poll(() => errors.some((text) => /integrity/i.test(text))).toBe(true);
  // the refused chunk ends on "page could not be loaded" with a reload, not on an endless spinner
  await expect(page.getByTestId('error.reload')).toBeVisible();
  await expect(page.getByTestId('security.build-hash')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.dataset.tampered)).toBeUndefined();
  // the link the check went through names the chunk's hash from index.html
  const link = page.locator('link[rel="modulepreload"][href*="/assets/screens-"]');
  await expect(link).toHaveAttribute('integrity', /^sha384-/);
  await expect(link).toHaveAttribute('crossorigin', 'anonymous');
});

test('the unchanged chunk loads through the checked link', async ({ page }) => {
  await page.goto('/security');
  await expect(page.getByTestId('security.build-hash')).toBeVisible();
  const table = await page.locator('meta[name="asset-integrity"]').getAttribute('content');
  const link = page.locator('link[rel="modulepreload"][href*="/assets/screens-"]');
  const href = new URL((await link.getAttribute('href'))!).pathname;
  expect(table!.split(' ')).toContain(href);
  expect(table!.split(' ')[table!.split(' ').indexOf(href) + 1]).toBe(await link.getAttribute('integrity'));
});
