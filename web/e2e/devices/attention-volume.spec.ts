// KOR-39 (ADR-064 section 4, E-19): the situation view stays small and usable with thousands of entries in one group,
// live (the hub's `attention` frames arrive while the page is open, applied in batches) and as history (the page opened
// anew pages the group from the hub, 50 entries at a time, while scrolling). Each group keeps at most 50 entries in the
// DOM (the window of the chat lists, HOT-05). The tasks wait for a computer that is offline: the hub derives
// "Stuck / The computer is not reachable" for every one of them. Every measurement is printed (`KOR-39 …`).
import { devices, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { setOnline } from '../helpers/director.ts';
import { freshUser, sendPrompt } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';

/** Tasks created (thousands: far more than a page of the hub and the window of the view). */
const TASKS = 1500;
/** Entries of one group in the DOM at most (`WINDOW` of `core/attention/view.ts`). */
const WINDOW = 50;
/** DOM element bound of the whole page: four groups (one with a full window of ~20 elements per entry) and the shell. */
const MAX_NODES = 3000;
/** Longest frame gap while the live burst arrives (the page keeps painting). */
const MAX_FRAME_GAP_MS = 500;
/** Main-thread time in long tasks within a 2 s quiet window once everything is shown. */
const MAX_IDLE_BUSY_MS = 300;
/** The user's requests share one rate limit (600 per minute, 03-api.md section 1) with the page: the first ones at
 *  once, then at most `RATE` per second. */
const BURST = 400;
const RATE = 7;
const CHUNK = 50;

async function instrument(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __long: { start: number; duration: number }[]; __gaps: number[] };
    w.__long = [];
    w.__gaps = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) w.__long.push({ start: entry.startTime, duration: entry.duration });
    }).observe({ type: 'longtask', buffered: true });
    let last = performance.now();
    const frame = (now: number) => {
      w.__gaps.push(now - last);
      last = now;
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  });
}

async function probe(page: Page, since: number) {
  return page.evaluate((from) => {
    const w = window as unknown as { __long: { start: number; duration: number }[]; __gaps: number[] };
    const busy = w.__long.filter((task) => task.start >= from).reduce((sum, task) => sum + task.duration, 0);
    const gap = Math.max(0, ...w.__gaps);
    w.__gaps = [];
    return { busy, gap, nodes: document.getElementsByTagName('*').length, now: performance.now() };
  }, since);
}

/** Creates the tasks through the API, paced below the rate limit. */
async function createAll(api: PageUser, org: string): Promise<void> {
  const started = Date.now();
  for (let done = 0; done < TASKS; done += CHUNK) {
    const titles = Array.from({ length: Math.min(CHUNK, TASKS - done) }, (_, index) => `Bulk task ${done + index + 1}`);
    await api.createTasks(org, org, titles);
    const due = done + CHUNK <= BURST ? 0 : started + ((done + CHUNK - BURST) / RATE) * 1000 - Date.now();
    if (due > 0) await new Promise((resolve) => setTimeout(resolve, due));
  }
}

const group = (page: Page) => page.getByTestId('attention.group.blocked');
const total = (page: Page) => group(page).evaluate((node) => Number((node as HTMLElement).dataset.total));
const rendered = (page: Page) => group(page).getByTestId('attention.item').count();

async function quiet(page: Page, label: string): Promise<void> {
  const start = await probe(page, 0);
  await page.waitForTimeout(2000);
  const end = await probe(page, start.now);
  console.log(`KOR-39 ${label}: nodes ${end.nodes}, busy ${Math.round(end.busy)} ms in 2 s`);
  expect(end.nodes).toBeLessThan(MAX_NODES);
  expect(end.busy).toBeLessThan(MAX_IDLE_BUSY_MS);
  // the composer at the top still takes input
  const prompt = page.getByTestId('composer.prompt');
  await prompt.fill(`still usable ${label}`);
  await expect(prompt).toHaveValue(`still usable ${label}`, { timeout: 2000 });
}

async function scenario(page: Page, baseURL: string, label: string): Promise<void> {
  test.setTimeout(600_000);
  await instrument(page);
  const user = await freshUser(page, baseURL, { poolReady: 2 });
  await sendPrompt(page, 'Warm up the computer');
  const api = await PageUser.open(page, user.recoveryCode);
  await setOnline(user.device, false);
  const bottom = page.getByTestId('shell.bottom.home');
  if (await bottom.isVisible()) await bottom.click();
  else await page.getByTestId('shell.nav.home').click();
  await expect(page.getByTestId('attention.screen')).toBeVisible();

  // live: thousands of records arrive while the page is open
  const before = await probe(page, 0);
  let settled = false;
  const creating = createAll(api, user.org).finally(() => (settled = true));
  let worst = 0;
  let nodes = 0;
  let most = 0;
  while (!settled) {
    await page.waitForTimeout(1000);
    const now = await probe(page, before.now);
    worst = Math.max(worst, now.gap);
    nodes = Math.max(nodes, now.nodes);
    most = Math.max(most, await rendered(page));
  }
  await creating;
  await expect.poll(() => total(page), { timeout: 120_000 }).toBeGreaterThanOrEqual(TASKS);
  await expect(page.getByTestId('attention.counter.blocked')).toHaveAttribute('data-count', /^\d{4,}$/);
  const after = await probe(page, before.now);
  console.log(
    `KOR-39 ${label} live: ${Math.round(after.now - before.now)} ms, max frame gap ${Math.round(Math.max(worst, after.gap))} ms, ` +
      `max nodes ${Math.max(nodes, after.nodes)}, max entries ${most}, entries in the group ${await total(page)}`,
  );
  expect(Math.max(worst, after.gap)).toBeLessThan(MAX_FRAME_GAP_MS);
  expect(Math.max(nodes, after.nodes)).toBeLessThan(MAX_NODES);
  expect(most).toBeLessThanOrEqual(WINDOW);
  const entry = group(page).getByTestId('attention.item').first();
  await expect(entry.getByTestId('attention.item.reason')).toHaveText('The computer is not reachable');
  await expect(entry.getByTestId('attention.retry')).toBeVisible();
  await quiet(page, `${label} live`);

  // history: the view opened anew pages the group from the hub while scrolling
  await page.reload();
  const loaded = Date.now();
  await page.getByTestId('login.submit').click();
  await expect(group(page).getByTestId('attention.item')).toHaveCount(WINDOW, { timeout: 60_000 });
  console.log(`KOR-39 ${label} history: first page after ${Date.now() - loaded} ms`);
  expect(await total(page)).toBe(WINDOW);
  // the end of the list comes into view: the next page loads and the window moves on
  await group(page).getByTestId('attention.end').scrollIntoViewIfNeeded();
  await expect.poll(() => total(page), { timeout: 30_000 }).toBeGreaterThanOrEqual(2 * WINDOW);
  // further on with "Show later entries" / "Load more": the DOM keeps one window
  for (let clicks = 0; clicks < 20 && (await total(page)) < 5 * WINDOW; clicks += 1) {
    await group(page).getByTestId('attention.more').click();
    await page.waitForTimeout(300);
    expect(await rendered(page)).toBeLessThanOrEqual(WINDOW);
  }
  expect(await total(page)).toBeGreaterThanOrEqual(5 * WINDOW);
  await expect(group(page).getByTestId('attention.earlier')).toBeVisible();
  // the window moved: its first entry is no longer the newest one
  const from = await group(page).evaluate((node) => Number((node as HTMLElement).dataset.from));
  expect(from).toBeGreaterThan(0);
  await group(page).getByTestId('attention.earlier').click();
  await expect
    .poll(() => group(page).evaluate((node) => Number((node as HTMLElement).dataset.from)))
    .toBeLessThan(from);
  await page.evaluate(() => window.scrollTo(0, 0));
  await quiet(page, `${label} history`);
}

test('stays small and usable with thousands of stuck tasks on desktop', async ({ page, baseURL }) => {
  await scenario(page, baseURL!, 'desktop');
});

test.describe('phone', () => {
  const { defaultBrowserType: _browser, ...pixel } = devices['Pixel 7'];
  test.use(pixel);

  test('stays small and usable with thousands of stuck tasks on a phone', async ({ page, baseURL }) => {
    await scenario(page, baseURL!, 'mobile');
  });
});
