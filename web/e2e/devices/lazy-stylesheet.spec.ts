// KOR-11 (AUD-02 NB-10; KOR-04 decision 7, 04-crypto.md section 9, 06-web.md section 10.2): a lazily loaded stylesheet
// carries integrity like a lazily loaded script. The monitoring charts' chunk needs its own stylesheet; the test changes
// the stylesheet's bytes on the way: the browser refuses it, its rules never apply and the chart chunk does not run –
// the chart shows its sparkline fallback, the screen stays (ACC-FINAL decision 11; before, the screen ended on the
// error page). Chromium only (playwright.config.ts; decisions KOR-11).
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { seedSamples } from '../helpers/monitoring.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

const STYLESHEET = /\/assets\/UPlotChart-[\w-]+\.css$/;

test('a lazily loaded stylesheet whose bytes were changed is refused and never applies', async ({ page, baseURL }) => {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  const served: string[] = [];
  await page.route(STYLESHEET, async (route) => {
    const response = await route.fetch();
    served.push(route.request().url());
    const body = `${await response.text()}\n:root { --tampered: ran; }\n`;
    await route.fulfill({ response, body });
  });
  const user = await freshUser(page, baseURL!, { poolReady: 2 });
  const task = await sendPrompt(page, 'Watch the load');
  const main = await mainSession(task);
  await walk(main.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await seedSamples(
    user.device,
    Array.from({ length: 8 }, (_, index) => ({
      entity_kind: 'device' as const,
      entity_id: user.device,
      age_s: 15 * index,
      cpu_ratio: 1 + index / 4,
    })),
  );
  await page.getByTestId('shell.nav.monitoring').click();
  const item = page.locator(`[data-testid="monitoring.device"][data-device="${user.device}"]`);
  await item.getByTestId('monitoring.device.charts-toggle').click();
  await expect.poll(() => served.length).toBeGreaterThan(0);
  // Chromium's own refusal (not the helper's rejection, which a network failure would cause as well)
  await expect.poll(() => errors.some((text) => /valid digest in the 'integrity' attribute/.test(text))).toBe(true);
  await expect(item.getByTestId('monitoring.chart.sparkline').first()).toBeVisible();
  await expect(page.getByTestId('error.reload')).toHaveCount(0);
  await expect(page.locator('.uplot')).toHaveCount(0);
  const tampered = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--tampered').trim(),
  );
  expect(tampered).toBe('');
  const link = page.locator('link[rel="stylesheet"][href*="/assets/UPlotChart-"]');
  await expect(link).toHaveAttribute('integrity', /^sha384-/);
  await expect(link).toHaveAttribute('crossorigin', 'anonymous');
});
