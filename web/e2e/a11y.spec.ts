// Keyboard and assistive-technology behaviour of 06-web.md section 12.3 on the desktop and both phone projects:
// the skip link is the first tab stop and moves the focus to the screen, the focus is always visible, the "More"
// sheet traps the focus and gives it back on Escape, `prefers-reduced-motion` stops transitions, and the connection
// state is announced in a polite live region. The axe-core checks of every route are part of the route sweeps
// (`responsive.spec.ts`, `devices/responsive.spec.ts`).
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { NARROW } from './helpers/layout-checks.ts';
import { onboard } from './helpers/onboard.ts';

test('the skip link is the first tab stop, visible when focused, and moves the focus to the screen', async ({
  page,
}) => {
  await page.goto('/login');
  await expect(page.getByTestId('login.submit')).toBeEnabled();
  await page.keyboard.press('Tab');
  const skip = page.getByTestId('shell.skip');
  await expect(skip).toBeFocused();
  const box = (await skip.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(44);
  await page.keyboard.press('Enter');
  await expect(page.locator('#content')).toBeFocused();
  // the next tab stop is inside the screen, and its focus is visible
  await page.keyboard.press('Tab');
  const focused = page.locator(':focus');
  expect(await focused.evaluate((element) => element.closest('#content') !== null)).toBe(true);
  expect(await focused.evaluate((element) => getComputedStyle(element).outlineStyle)).toBe('solid');
});

test('the More sheet traps the focus and returns it on Escape; the connection is announced', async ({
  page,
  baseURL,
}) => {
  await page.setViewportSize(NARROW);
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await expect(page.getByTestId('shell.connection.announce')).toHaveAttribute('aria-live', 'polite');
  await expect(page.getByTestId('shell.connection.announce')).toHaveText(/Connect|live/);
  const more = page.getByTestId('shell.bottom.more');
  await more.focus();
  await page.keyboard.press('Enter');
  const sheet = page.getByTestId('shell.more');
  await expect(sheet).toHaveAttribute('aria-modal', 'true');
  const inside = () => page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null);
  expect(await inside()).toBe(true);
  for (let step = 0; step < 12; step += 1) {
    await page.keyboard.press(step % 3 === 0 ? 'Shift+Tab' : 'Tab');
    expect(await inside()).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveCount(0);
  await expect(more).toBeFocused();
});

test('an open on-screen keyboard hides the bottom navigation and keeps the composer in view', async ({
  page,
  baseURL,
}) => {
  // Chromium's emulation opens no keyboard: a stand-in visual viewport that the test shrinks like a keyboard would
  await page.addInitScript({
    content: `const viewport = new EventTarget();
      viewport.height = window.innerHeight;
      viewport.offsetTop = 0;
      Object.defineProperty(window, 'visualViewport', { value: viewport, configurable: true });`,
  });
  await page.setViewportSize(NARROW);
  await addAuthenticator(page);
  await onboard(page, { origin: baseURL! });
  await expect(page.getByTestId('shell.bottom')).toBeVisible();
  await page.getByTestId('composer.prompt').focus();
  await page.evaluate(() => {
    const viewport = window.visualViewport as unknown as EventTarget & { height: number };
    viewport.height = window.innerHeight - 300;
    viewport.dispatchEvent(new Event('resize'));
  });
  const layout = page.getByTestId('shell.layout');
  await expect(layout).toHaveAttribute('data-keyboard', '300');
  expect(await layout.evaluate((element) => getComputedStyle(element).paddingBottom)).toBe('300px');
  await expect(page.getByTestId('shell.bottom')).toHaveCount(0);
  await expect(page.getByTestId('composer.prompt')).toBeFocused();
  await expect(page.getByTestId('composer.prompt')).toBeInViewport();
});

test('prefers-reduced-motion stops transitions and animations', async ({ newContext }) => {
  const still = await (await newContext({ reducedMotion: 'reduce' })).newPage();
  await still.goto('/login');
  await expect(still.getByTestId('login.page')).toBeVisible();
  expect(await still.evaluate(() => getComputedStyle(document.body).transitionDuration)).toBe('1e-05s');
  const moving = await (await newContext({ reducedMotion: 'no-preference' })).newPage();
  await moving.goto('/login');
  await expect(moving.getByTestId('login.page')).toBeVisible();
  expect(await moving.evaluate(() => getComputedStyle(document.body).transitionDuration)).toBe('0s');
});
