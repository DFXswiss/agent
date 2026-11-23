// HOT-05: a task with 5 000 `session.file` events (each with a patch, ordinary items in between) stays usable, as
// history (the page is opened afterwards) and as a live burst (the page is open), on desktop and on a phone: the
// composer reacts, the page scrolls, the DOM stays below `MAX_NODES` and the main thread is not kept busy. Bounds
// and measurements: `docs/platform/decisions/HOT-05.md`. Every measurement is printed (`HOT-05 …`) for the report.
import { devices, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { type SessionEvent, sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

/** File events, an ordinary item after every `RUN` files (1 000 rows: more than the rendered window of 150). */
const FILES = 5000;
const RUN = 10;
/** Rows once every event is shown: 500 groups and 500 outputs, less what the store evicted (5 000 events per scope,
 * least recently written first: the prompt and the first 501 events). */
const MIN_ROWS = (2 * FILES) / RUN - 120;
const BATCH = 500;
/** DOM element bound of the whole page: the rendered window (150 rows of ~20 elements) plus the app shell. */
const MAX_NODES = 4000;
/** Main-thread time in long tasks within a 2 s quiet window once everything is shown. */
const MAX_IDLE_BUSY_MS = 300;
/** Longest frame gap while the live burst is rendered (the page must keep painting). */
const MAX_FRAME_GAP_MS = 500;
/** Share of the live burst's wall time spent in long tasks (the main thread is not kept busy). */
const MAX_BURST_BUSY = 0.5;
/** Time until the composer shows typed text. */
const MAX_INPUT_MS = 2000;

const PATCH = Array.from({ length: 20 }, (_, line) => `+line ${line} of a checked-out file`).join('\n');

function events(offset: number): SessionEvent[] {
  const list: SessionEvent[] = [];
  for (let index = 0; index < FILES; index += 1) {
    const n = offset + index;
    list.push({ kind: 'session.file', body: { path: `repo/dir${n % 37}/file${n}.txt`, op: 'write', patch: PATCH } });
    if ((index + 1) % RUN === 0)
      list.push({
        kind: 'session.output',
        body: { message_id: `m${n}`, role: 'assistant', turn: 1, text: `Run ${n}` },
      });
  }
  return list;
}

async function store(session: string, list: SessionEvent[]): Promise<void> {
  for (let index = 0; index < list.length; index += BATCH)
    await sessionEvents(session, list.slice(index, index + BATCH));
}

/** Records long tasks and frame gaps from the start of the page (read by `probe`). */
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

/** Long-task time since `since` (ms on the page clock), the longest frame gap since the last call, the DOM size. */
async function probe(page: Page, since: number) {
  return page.evaluate((from) => {
    const w = window as unknown as { __long: { start: number; duration: number }[]; __gaps: number[] };
    const busy = w.__long.filter((task) => task.start >= from).reduce((sum, task) => sum + task.duration, 0);
    const gap = Math.max(0, ...w.__gaps);
    w.__gaps = [];
    return { busy, gap, nodes: document.getElementsByTagName('*').length, now: performance.now() };
  }, since);
}

/** Rows of the timeline (rendered or not). */
const rows = (page: Page) => page.getByTestId('chat.timeline').evaluate((node) => Number(node.dataset.total));

/** Waits until the last event is shown and the timeline has all its rows. */
async function shownAll(page: Page): Promise<void> {
  await expect(page.getByTestId('chat.output').filter({ hasText: `Run ${FILES - 1}` })).toBeVisible({
    timeout: 120_000,
  });
  await expect.poll(() => rows(page), { timeout: 120_000 }).toBeGreaterThanOrEqual(MIN_ROWS);
}

async function usable(page: Page, label: string): Promise<void> {
  // the composer reacts and the page scrolls
  const prompt = page.getByTestId('chat.composer.prompt');
  const typed = Date.now();
  await prompt.fill(`still usable ${label}`);
  await expect(prompt).toHaveValue(`still usable ${label}`, { timeout: 2000 });
  const input = Date.now() - typed;
  expect(input).toBeLessThan(MAX_INPUT_MS);
  // scrolling moves the page
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const bottom = await page.evaluate(() => window.scrollY);
  expect(bottom).toBeGreaterThan(0);
  await page.mouse.wheel(0, -1500);
  await expect.poll(() => page.evaluate(() => window.scrollY), { timeout: 2000 }).toBeLessThan(bottom);
  await page.getByTestId('chat.files').first().scrollIntoViewIfNeeded();
  await page.getByTestId('chat.files.toggle').first().click();
  await expect(page.getByTestId('chat.files.list').first().getByTestId('chat.file')).toHaveCount(RUN);
  await page.getByTestId('chat.files.toggle').first().click();
  // quiet window: the main thread is not kept busy
  const start = await probe(page, 0);
  await page.waitForTimeout(2000);
  const end = await probe(page, start.now);
  console.log(`HOT-05 ${label}: input ${input} ms, nodes ${end.nodes}, busy ${Math.round(end.busy)} ms in 2 s`);
  expect(end.nodes).toBeLessThan(MAX_NODES);
  expect(end.busy).toBeLessThan(MAX_IDLE_BUSY_MS);
}

async function scenario(page: Page, baseURL: string, label: string): Promise<void> {
  test.setTimeout(600_000);
  await instrument(page);
  await freshUser(page, baseURL, { poolReady: 2 });
  const task = await sendPrompt(page, 'Check out the repository');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');

  // live: the burst arrives while the page is open
  const before = await probe(page, 0);
  const posting = store(session.session_id, events(0));
  let worst = 0;
  let nodes = 0;
  let settled = false;
  void posting.then(() => (settled = true));
  while (!settled) {
    await page.waitForTimeout(500);
    const now = await probe(page, before.now);
    worst = Math.max(worst, now.gap);
    nodes = Math.max(nodes, now.nodes);
  }
  await posting;
  await shownAll(page);
  const after = await probe(page, before.now);
  const gap = Math.max(worst, after.gap);
  const busy = after.busy / (after.now - before.now);
  console.log(
    `HOT-05 ${label} live: ${Math.round(after.now - before.now)} ms, busy ${Math.round(busy * 100)} %, ` +
      `max frame gap ${Math.round(gap)} ms, max nodes ${nodes}, rows ${await rows(page)}`,
  );
  expect(gap).toBeLessThan(MAX_FRAME_GAP_MS);
  expect(busy).toBeLessThan(MAX_BURST_BUSY);
  expect(nodes).toBeLessThan(MAX_NODES);
  // the window: far more rows than rendered
  expect(await rows(page)).toBeGreaterThan(await page.getByTestId('chat.item').count());
  await expect(page.getByTestId('chat.earlier')).toBeVisible();
  await usable(page, `${label} live`);

  // history: the same task opened anew loads every event from the hub
  await page.reload();
  const loaded = Date.now();
  await page.getByTestId('login.submit').click();
  await shownAll(page);
  console.log(`HOT-05 ${label} history: shown after ${Date.now() - loaded} ms`);
  await usable(page, `${label} history`);
}

test('stays usable with 5 000 file events on desktop', async ({ page, baseURL }) => {
  await scenario(page, baseURL!, 'desktop');
});

test.describe('phone', () => {
  const { defaultBrowserType: _browser, ...pixel } = devices['Pixel 7'];
  test.use(pixel);

  test('stays usable with 5 000 file events on a phone', async ({ page, baseURL }) => {
    await scenario(page, baseURL!, 'mobile');
  });
});
