import { expect, test, type Locator, type Page } from '@playwright/test';
import { EMPTY_URL, FILLED_URL, NOW } from './servers';

/**
 * Screenshot baselines of every page of the local dashboard (`agent dashboard`).
 *
 * The pages are served by the real dashboard server on a seeded home
 * (e2e/dashboard_server.py): FILLED_URL with data in every section, EMPTY_URL
 * with none. Server and browser clocks are frozen at NOW, so elapsed times and
 * countdowns are the same on every run. A local review aid, not run in CI; see
 * CONTRIBUTING.md, "Visual regression tests (Playwright)".
 */

const SECTIONS = ['usage', 'sessions', 'tasks', 'rounds', 'agents', 'checks', 'gates', 'work', 'pings'] as const;

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date(NOW));
});

async function snap(page: Page, name: string): Promise<void> {
  await expect(page.locator('#err')).toBeHidden();
  await expect(page).toHaveScreenshot(name, { fullPage: true });
}

async function fitsViewport(page: Page): Promise<void> {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBe(true);
}

// A loaded page shows the device id; the section tables are filled by the same refresh.
async function openSection(page: Page, base: string, section: string): Promise<void> {
  await page.goto(`${base}/${section}`);
  await expect(page.locator('#dev')).not.toBeEmpty();
}

async function openMain(page: Page, base: string): Promise<void> {
  await page.goto(`${base}/`);
  await expect(page.locator('#dev')).not.toBeEmpty();
  await expect(page.locator('#orders .order').first()).toBeVisible();
}

// The blocked seed and the duplicate seed share the title "Add a widget" and the
// Waiting label. The blocked seed is the row aged 50 min (e2e/dashboard_server.py).
function blockedOrder(page: Page): Locator {
  return page
    .locator('#orders .order', { has: page.locator('b', { hasText: /^Add a widget$/ }) })
    .filter({ has: page.locator('.label', { hasText: /^Waiting$/ }) })
    .filter({ hasText: '50 min' });
}

async function openBlockedOrder(page: Page): Promise<void> {
  await blockedOrder(page).click();
  await expect(page.locator('#od-question')).toBeVisible();
  await expect(page.locator('#od-override-check option')).not.toHaveCount(0);
}

test.describe('filled dashboard', () => {
  test('main page', async ({ page }) => {
    await openMain(page, FILLED_URL);
    await page.locator('#seg button[data-f="all"]').click();
    await expect(page.locator('#orders .order')).toHaveCount(6);
    await snap(page, 'main.png');
  });

  test('main page with a selected order waiting for an answer', async ({ page }) => {
    await openMain(page, FILLED_URL);
    await openBlockedOrder(page);
    await snap(page, 'main-order-selected.png');
  });

  test('order events view', async ({ page }) => {
    await page.goto(`${FILLED_URL}/#events`);
    await expect(page.locator('#dev')).not.toBeEmpty();
    await expect(page.locator('#order-events thead th').first()).toBeVisible();
    await expect(page.locator('#order-events button[data-event]').first()).toBeVisible();
    expect(
      await page.evaluate(() => {
        const table = document.querySelector('#order-events');
        const frame = table?.closest('.card');
        const main = document.querySelector('#main');
        if (!(frame instanceof HTMLElement) || !(main instanceof HTMLElement)) return false;
        const overflow = getComputedStyle(frame).overflowX;
        return (
          (overflow === 'auto' || overflow === 'scroll') &&
          frame.scrollWidth >= frame.clientWidth &&
          frame.getBoundingClientRect().right <= main.getBoundingClientRect().right + 1
        );
      }),
    ).toBe(true);
    await snap(page, 'order-events.png');
  });

  test('main page with a selected order on a closed foldable phone', async ({ page }) => {
    await page.setViewportSize({ width: 382, height: 580 });
    await openMain(page, FILLED_URL);
    await openBlockedOrder(page);
    await fitsViewport(page);
    await snap(page, 'main-order-selected-closed.png');
  });

  test('main page with a selected order on an open foldable phone', async ({ page }) => {
    await page.setViewportSize({ width: 669, height: 867 });
    await openMain(page, FILLED_URL);
    await openBlockedOrder(page);
    await fitsViewport(page);
    await snap(page, 'main-order-selected-open.png');
  });

  test('main page with a selected order folded as a book', async ({ page }) => {
    await page.setViewportSize({ width: 951, height: 585 });
    await page.goto(`${FILLED_URL}/?fold=book`);
    await expect(page.locator('#dev')).not.toBeEmpty();
    await expect(page.locator('#orders .order').first()).toBeVisible();
    await openBlockedOrder(page);
    await fitsViewport(page);
    await snap(page, 'main-order-selected-book.png');
  });

  test('main page at a low height', async ({ page }) => {
    await page.setViewportSize({ width: 594, height: 368 });
    await openMain(page, FILLED_URL);
    await fitsViewport(page);
    await snap(page, 'main-low.png');
  });

  test('section tasks on a phone', async ({ page }) => {
    await page.setViewportSize({ width: 382, height: 580 });
    await openSection(page, FILLED_URL, 'tasks');
    await expect(page.locator('#tasks tbody tr').first().locator('td').nth(1)).toBeVisible();
    await fitsViewport(page);
    await snap(page, 'section-tasks-phone.png');
  });

  test('new order page', async ({ page }) => {
    await page.goto(`${FILLED_URL}/orders/new`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#order-submit')).toBeVisible();
    await snap(page, 'new-order.png');
  });

  test('new order page on a phone', async ({ page }) => {
    await page.setViewportSize({ width: 382, height: 580 });
    await page.goto(`${FILLED_URL}/orders/new`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#order-submit')).toBeVisible();
    await fitsViewport(page);
    await snap(page, 'new-order-phone.png');
  });

  test('new order page with a filled form and a skipped check', async ({ page }) => {
    await page.goto(`${FILLED_URL}/orders/new`);
    await page.waitForLoadState('networkidle');
    await page.fill('#os-repo', 'example/project');
    await page.fill('#os-title', 'Add a CSV export');
    await page.fill('#os-base', 'develop');
    await page.fill('#os-max-rounds', '3');
    await page.fill('#os-body', 'Add a CSV export to the report page.');
    await page.click('#os-add-skip');
    await page.fill('.os-skip-reason', 'Checked by hand');
    await page.locator('.os-skip-reason').blur();
    await snap(page, 'new-order-filled.png');
  });

  for (const section of SECTIONS) {
    test(`section ${section}`, async ({ page }) => {
      await openSection(page, FILLED_URL, section);
      await expect(page.locator(`#${section} tbody tr`).first().locator('td').nth(1)).toBeVisible();
      await snap(page, `section-${section}.png`);
    });
  }

  test('section usage with one provider opened', async ({ page }) => {
    await openSection(page, FILLED_URL, 'usage');
    await page.locator('.usage-provider[data-provider="driver-w"] > summary').click();
    await expect(page.locator('.usage-provider[data-provider="driver-w"]')).toHaveAttribute('open', '');
    await snap(page, 'section-usage-provider-open.png');
  });

  test('section usage detailed and sorted by used', async ({ page }) => {
    await openSection(page, FILLED_URL, 'usage');
    await page.check('#usage-detailed');
    await page.check('#usage-sort');
    await expect(page.locator('.usage-provider:not([open])')).toHaveCount(0);
    await snap(page, 'section-usage-detailed-sorted.png');
  });

  test('section sessions with an own session selected', async ({ page }) => {
    await openSection(page, FILLED_URL, 'sessions');
    await page.locator('tr.session-row', { has: page.locator('td', { hasText: /^control$/ }) })
      .filter({ has: page.locator('td', { hasText: /^interactive$/ }) })
      .locator('td').first()
      .click({ position: { x: 4, y: 4 } });
    await expect(page.locator('#ctl-start')).toBeVisible();
    await snap(page, 'section-sessions-own-selected.png');
  });

  test('section sessions with a session of another device selected', async ({ page }) => {
    await openSection(page, FILLED_URL, 'sessions');
    await page.locator('tr.session-row', { has: page.locator('td', { hasText: /^watch$/ }) })
      .locator('td').first()
      .click({ position: { x: 4, y: 4 } });
    await expect(page.locator('#detail-controls .watch-note')).toBeVisible();
    await snap(page, 'section-sessions-foreign-selected.png');
  });
});

test.describe('empty dashboard', () => {
  test('main page', async ({ page }) => {
    await page.goto(`${EMPTY_URL}/`);
    await expect(page.locator('#dev')).not.toBeEmpty();
    await expect(page.locator('#orders-note')).toHaveText('Orders are not enabled on this device.');
    await snap(page, 'empty-main.png');
  });

  test('new order page', async ({ page }) => {
    await page.goto(`${EMPTY_URL}/orders/new`);
    await expect(page.locator('#orders-note')).toHaveText('Orders are not enabled on this device.');
    await snap(page, 'empty-new-order.png');
  });

  test('section usage', async ({ page }) => {
    await openSection(page, EMPTY_URL, 'usage');
    await expect(page.locator('#usage tbody')).toContainText('No subscription inventory');
    await snap(page, 'empty-section-usage.png');
  });

  test('section sessions', async ({ page }) => {
    await openSection(page, EMPTY_URL, 'sessions');
    await expect(page.locator('#sessions tbody td')).toHaveText('—');
    await snap(page, 'empty-section-sessions.png');
  });
});
