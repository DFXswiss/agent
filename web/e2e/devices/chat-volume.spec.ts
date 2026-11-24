// HOT-05: a task with 5 000 `session.file` events (each with a patch, ordinary items in between) stays usable, as
// history (the page is opened afterwards) and as a live burst (the page is open), on desktop and on a phone: the
// composer reacts, the page scrolls, the DOM stays below `MAX_NODES` and the main thread is not kept busy. The same
// holds for every other chat kind in large numbers (`mixed`: 1 500 long messages, 1 500 tool calls with 8 KB input
// and output, 1 000 subagent messages). Bounds
// and measurements: `docs/platform/decisions/HOT-05.md`. Every measurement is printed (`HOT-05 …`) for the report.
import { devices, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { mainSession, walk } from '../helpers/director.ts';
import { childSession, type SessionEvent, sessionEvents } from '../helpers/sessions.ts';
import { freshUser, sendPrompt, START_STEPS } from '../helpers/tasks.ts';

/** File events, an ordinary item after every `RUN` files (1 000 rows: more than the rendered window of 100). */
const FILES = 5000;
const RUN = 10;
/** Rows once every event is shown: 500 groups and 500 outputs, less what the store evicted (5 000 events per scope,
 * least recently written first: the prompt and the first 501 events). */
const MIN_ROWS = (2 * FILES) / RUN - 120;
const BATCH = 500;
/** DOM element bound of the whole page: the rendered window (100 rows of ~20 elements) plus the app shell. */
const MAX_NODES = 4000;
/** The same with rich rows (Markdown messages of ~45 elements): up to 150 rows (the window grows by `PAGE` before
 * it moves) plus the app shell. */
const MAX_NODES_RICH = 9000;
/** Main-thread time in long tasks within a 2 s quiet window once everything is shown. */
const MAX_IDLE_BUSY_MS = 300;
/** Longest frame gap while the live burst is rendered (the page must keep painting). */
const MAX_FRAME_GAP_MS = 500;
/** The same with rich rows: a batch of 500 events moves the window and mounts up to 100 new Markdown and tool rows
 * in one frame; measured 417 ms on an idle test device and 750 ms on a loaded one, in the coverage build. */
const MAX_FRAME_GAP_RICH_MS = 1000;
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

async function usable(page: Page, label: string, files = true): Promise<void> {
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
  if (files) {
    await page.getByTestId('chat.files').first().scrollIntoViewIfNeeded();
    await page.getByTestId('chat.files.toggle').first().click();
    await expect(page.getByTestId('chat.files.list').first().getByTestId('chat.file')).toHaveCount(RUN);
    await page.getByTestId('chat.files.toggle').first().click();
  }
  // quiet window: the main thread is not kept busy
  const start = await probe(page, 0);
  await page.waitForTimeout(2000);
  const end = await probe(page, start.now);
  console.log(`HOT-05 ${label}: input ${input} ms, nodes ${end.nodes}, busy ${Math.round(end.busy)} ms in 2 s`);
  expect(end.nodes).toBeLessThan(files ? MAX_NODES : MAX_NODES_RICH);
  expect(end.busy).toBeLessThan(MAX_IDLE_BUSY_MS);
}

/** Opens a task with a working main session; returns its session ID. */
async function openTask(page: Page, baseURL: string): Promise<string> {
  test.setTimeout(600_000);
  await instrument(page);
  await freshUser(page, baseURL, { poolReady: 2 });
  const task = await sendPrompt(page, 'Check out the repository');
  const session = await mainSession(task);
  await walk(session.session_id, [...START_STEPS, 'working'], { intervalMs: 20 });
  await expect(page.getByTestId('countdown.summary')).toContainText('Ready in');
  return session.session_id;
}

/** A live burst (`post`) while the page is open, measured until `done`; then the page must be usable. */
async function burst(
  page: Page,
  label: string,
  post: () => Promise<void>,
  done: (page: Page) => Promise<void>,
  files: boolean,
): Promise<void> {
  const [maxNodes, maxGap] = files ? [MAX_NODES, MAX_FRAME_GAP_MS] : [MAX_NODES_RICH, MAX_FRAME_GAP_RICH_MS];
  const before = await probe(page, 0);
  const posting = post();
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
  await done(page);
  const after = await probe(page, before.now);
  const gap = Math.max(worst, after.gap);
  const busy = after.busy / (after.now - before.now);
  console.log(
    `HOT-05 ${label} live: ${Math.round(after.now - before.now)} ms, busy ${Math.round(busy * 100)} %, ` +
      `max frame gap ${Math.round(gap)} ms, max nodes ${Math.max(nodes, after.nodes)}, rows ${await rows(page)}`,
  );
  expect(gap).toBeLessThan(maxGap);
  expect(busy).toBeLessThan(MAX_BURST_BUSY);
  expect(Math.max(nodes, after.nodes)).toBeLessThan(maxNodes);
  // the window: far more rows than rendered
  expect(await rows(page)).toBeGreaterThan(await page.getByTestId('chat.item').count());
  await expect(page.getByTestId('chat.earlier')).toBeVisible();
  await usable(page, `${label} live`, files);

  // history: the same task opened anew loads every event from the hub
  await page.reload();
  const loaded = Date.now();
  await page.getByTestId('login.submit').click();
  await done(page);
  console.log(`HOT-05 ${label} history: shown after ${Date.now() - loaded} ms`);
  await usable(page, `${label} history`, files);
}

async function scenario(page: Page, baseURL: string, label: string): Promise<void> {
  const session = await openTask(page, baseURL);
  await burst(page, label, () => store(session, events(0)), shownAll, true);
}

/** Every other chat kind in large numbers: long messages, tool calls with large input and output, a subagent. */
const MESSAGES = 1500;
const TOOLS = 1500;
const CHILD = 1000;
const TEXT = `## Step\n\n${'Some **Markdown** text of a long assistant message. '.repeat(40)}`;
const LARGE = 'x'.repeat(8000);
/** The contract's maximum of a tool input or result (64 KiB): the last tool call, clipped in the chat. */
const HUGE = 'z'.repeat(64 * 1024);

async function mixed(page: Page, baseURL: string, label: string): Promise<void> {
  const main = await openTask(page, baseURL);
  const child = await childSession(main);
  const message = (n: number): SessionEvent => ({
    kind: 'session.output',
    body: { message_id: `m${n}`, role: 'assistant', turn: 1, text: `${TEXT} Msg ${n}` },
  });
  const tool = (n: number, payload = LARGE): SessionEvent => ({
    kind: 'session.tool',
    body: {
      tool_use_id: `t${n}`,
      name: 'Bash',
      input_summary: `cat ${n}`,
      input: payload,
      result_summary: payload,
      is_error: false,
      duration_ms: 10,
    },
  });
  const childOutput = (n: number): SessionEvent => ({
    kind: 'session.output',
    body: { message_id: `c${n}`, role: 'assistant', turn: 1, text: `Child ${n}` },
  });
  const range = (count: number, from: number) => Array.from({ length: count }, (_, index) => from + index);
  const post = async () => {
    const step = 250;
    for (let from = 0; from < MESSAGES; from += step) {
      await sessionEvents(
        main,
        range(step, from).flatMap((n) => [message(n), tool(n)]),
      );
      if (from < CHILD) await sessionEvents(child, range(step, from).map(childOutput));
    }
    await sessionEvents(main, [tool(TOOLS, HUGE)]);
  };
  const done = async (shown: Page) => {
    await expect(shown.getByTestId('chat.tool.summary').filter({ hasText: `cat ${TOOLS}` })).toBeVisible({
      timeout: 120_000,
    });
    await expect
      .poll(() => rows(shown), { timeout: 120_000 })
      .toBeGreaterThanOrEqual(MESSAGES + TOOLS + 1 + CHILD - 10);
  };
  await burst(page, `${label} mixed`, post, done, false);
  // the largest tool call opens with its input and output clipped, the rest behind "Show more"
  const last = page.getByTestId('chat.tool').last();
  await last.locator('summary').click();
  await expect(last.getByTestId('chat.tool.input')).toHaveText(HUGE.slice(0, 20_000));
  await expect(last.getByTestId('chat.more')).toHaveCount(2);
  await last.getByTestId('chat.more').first().click();
  await expect(last.getByTestId('chat.tool.input')).toHaveText(HUGE.slice(0, 40_000));
}

test('stays usable with 5 000 file events on desktop', async ({ page, baseURL }) => {
  await scenario(page, baseURL!, 'desktop');
});

test('stays usable with thousands of messages, large tool calls and subagent items on desktop', async ({
  page,
  baseURL,
}) => {
  await mixed(page, baseURL!, 'desktop');
});

test.describe('phone', () => {
  const { defaultBrowserType: _browser, ...pixel } = devices['Pixel 7'];
  test.use(pixel);

  test('stays usable with 5 000 file events on a phone', async ({ page, baseURL }) => {
    await scenario(page, baseURL!, 'mobile');
  });

  test('stays usable with thousands of messages, large tool calls and subagent items on a phone', async ({
    page,
    baseURL,
  }) => {
    await mixed(page, baseURL!, 'mobile');
  });
});
