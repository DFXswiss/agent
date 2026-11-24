// HOT-16 (06-web.md section 12.2): screens other than sign-in load on first use. The first switch to such a screen shows
// its frame at once, never an empty page; when its chunk cannot be fetched (the link to the hub is down at that moment)
// the shell stays and the screen area says the page could not be loaded, with a reload.
import { expect, test } from './fixtures/test.ts';

const CHUNK = /\/assets\/RecoverScreen-[\w-]+\.js$/;

test('the first switch to a lazily loaded screen shows its frame at once, then the screen', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(CHUNK, async (route) => {
    await held;
    await route.continue();
  });
  await page.goto('/login');
  await expect(page.getByTestId('login.submit')).toBeEnabled();
  await page.getByTestId('login.recover').click();
  await expect(page.getByTestId('screen.loading')).toBeVisible();
  await expect(page.getByTestId('shell.footer.security')).toBeVisible();
  release();
  await expect(page.getByTestId('screen.loading')).toHaveCount(0);
  await expect(page).toHaveURL(/\/recover$/);
  await expect(page.getByTestId('recover.page')).toBeVisible();
});

test('a screen whose chunk cannot be fetched says so inside the shell, with a reload', async ({ page }) => {
  await page.route(CHUNK, (route) => route.abort('connectionreset'));
  await page.goto('/login');
  await expect(page.getByTestId('login.submit')).toBeEnabled();
  await page.getByTestId('login.recover').click();
  await expect(page.getByTestId('screen.unavailable')).toBeVisible();
  await expect(page.getByTestId('error.reload')).toBeVisible();
  await expect(page.getByTestId('shell.footer.security')).toBeVisible();
  await expect(page.getByTestId('error.page')).toHaveCount(0);
  // "Reload" loads the page again; the chunk is still unreachable, so the screen says so again
  const reloaded = page.waitForEvent('load');
  await page.getByTestId('error.reload').click();
  await reloaded;
  await expect(page).toHaveURL(/\/recover$/);
  await expect(page.getByTestId('screen.unavailable')).toBeVisible();
});
