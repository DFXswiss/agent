// KOR-52 (ADR-095 section 5, E-02, E-19): the situation view by person stays small and usable with thousands of main
// agents of one person and hundreds of people. The person rows come from the hub 50 at a time and at most 50 are in
// the DOM; a person's agents too, paged while scrolling ("Show later" / "Load more"), never a full list. The user's
// own 2 000 agents are created through the API (sealed titles, real tasks); the 300 further people own one open task
// each, written by the director (`seedPeople`). Every measurement is printed (`KOR-52 …`).
import { devices, type Locator, type Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { seedPeople } from '../helpers/director.ts';
import { freshUser } from '../helpers/tasks.ts';
import { PageUser } from '../helpers/user.ts';
import { visit } from '../helpers/whiteboard.ts';

/** The user's own main agents (far more than a page of the hub and the window of the view). */
const AGENTS = 2000;
/** Further people with an open main agent each. */
const PEOPLE = 300;
/** Rows of a list in the DOM at most (`WINDOW` of `core/attention/view.ts`). */
const WINDOW = 50;
/** DOM element bound of the whole page: one window of people and one of agents (~25 elements each) and the shell. */
const MAX_NODES = 4000;
/** Main-thread time in long tasks within a 2 s quiet window once everything is shown. */
const MAX_IDLE_BUSY_MS = 300;
/** The user's requests share one rate limit (600 per minute, 03-api.md section 1): the first ones at once, then at
 *  most `RATE` per second. */
const BURST = 400;
const RATE = 7;
const CHUNK = 50;

async function instrument(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __long: { start: number; duration: number }[] };
    w.__long = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) w.__long.push({ start: entry.startTime, duration: entry.duration });
    }).observe({ type: 'longtask', buffered: true });
  });
}

async function probe(page: Page, since: number) {
  return page.evaluate((from) => {
    const w = window as unknown as { __long: { start: number; duration: number }[] };
    const busy = w.__long.filter((task) => task.start >= from).reduce((sum, task) => sum + task.duration, 0);
    return { busy, nodes: document.getElementsByTagName('*').length, now: performance.now() };
  }, since);
}

/** Creates the user's tasks through the API, paced below the rate limit. */
async function createAll(api: PageUser, org: string, wg: string): Promise<void> {
  const started = Date.now();
  for (let done = 0; done < AGENTS; done += CHUNK) {
    const titles = Array.from(
      { length: Math.min(CHUNK, AGENTS - done) },
      (_, index) => `Agent task ${done + index + 1}`,
    );
    await api.createTasks(org, wg, titles);
    const due = done + CHUNK <= BURST ? 0 : started + ((done + CHUNK - BURST) / RATE) * 1000 - Date.now();
    if (due > 0) await new Promise((resolve) => setTimeout(resolve, due));
  }
}

const number = (list: Locator, name: string) =>
  list.evaluate((node, key) => Number((node as HTMLElement).dataset[key]), name);

async function quiet(page: Page, label: string): Promise<void> {
  const start = await probe(page, 0);
  await page.waitForTimeout(2000);
  const end = await probe(page, start.now);
  console.log(`KOR-52 ${label}: nodes ${end.nodes}, busy ${Math.round(end.busy)} ms in 2 s`);
  expect(end.nodes).toBeLessThan(MAX_NODES);
  expect(end.busy).toBeLessThan(MAX_IDLE_BUSY_MS);
}

/** Pages a windowed list with "Show later" / "Load more" until it holds `least` rows; the DOM keeps one window. */
async function pageThrough(list: Locator, rows: Locator, more: Locator, least: number): Promise<void> {
  for (let clicks = 0; clicks < 40 && (await number(list, 'total')) < least; clicks += 1) {
    await more.click();
    await list.page().waitForTimeout(200);
    expect(await rows.count()).toBeLessThanOrEqual(WINDOW);
  }
  expect(await number(list, 'total')).toBeGreaterThanOrEqual(least);
}

async function scenario(page: Page, baseURL: string, label: string): Promise<void> {
  test.setTimeout(900_000);
  await instrument(page);
  const user = await freshUser(page, baseURL, { poolReady: 2 });
  const api = await PageUser.open(page, user.recoveryCode);
  const wg = (await api.log(user.org)).state.defaultWg;
  const created = Date.now();
  await createAll(api, user.org, wg);
  const owners = await seedPeople(user.org, wg, PEOPLE);
  expect(owners).toHaveLength(PEOPLE);
  console.log(`KOR-52 ${label}: ${AGENTS} agents and ${PEOPLE} people in ${Date.now() - created} ms`);

  const opened = Date.now();
  await visit(page, `/o/${user.org}?by=person`);
  const people = page.getByTestId('agents.people');
  const personRows = people.getByTestId('agents.person');
  await expect(personRows).toHaveCount(WINDOW, { timeout: 60_000 });
  console.log(`KOR-52 ${label}: first page of people after ${Date.now() - opened} ms`);
  expect(await number(people, 'total')).toBe(WINDOW);

  // the user's own row is first, with all agents counted; opened, it pages the agents from the hub
  const own = personRows.first();
  await expect(own).toHaveAttribute('data-owner', api.userId);
  await expect(own.getByTestId('agents.person.count.agents')).toHaveText(/^2[,.’ ]?000$/);
  await own.getByTestId('agents.person.toggle').click();
  const agents = own.getByTestId('agents.list');
  const agentRows = agents.getByTestId('agents.item');
  await expect(agentRows).toHaveCount(WINDOW, { timeout: 60_000 });
  expect(await number(agents, 'total')).toBe(WINDOW);
  // the end of the list comes into view: the next page loads and the window moves on
  await agents.getByTestId('agents.end').scrollIntoViewIfNeeded();
  await expect.poll(() => number(agents, 'total'), { timeout: 30_000 }).toBeGreaterThanOrEqual(2 * WINDOW);
  await pageThrough(agents, agentRows, agents.getByTestId('agents.more'), 5 * WINDOW);
  expect(await number(agents, 'from')).toBeGreaterThan(0);
  await agents.getByTestId('agents.earlier').click();
  await quiet(page, `${label} agents`);
  await own.getByTestId('agents.person.toggle').click();
  await expect(agentRows).toHaveCount(0);

  // hundreds of people: paged by the hub, one window in the DOM
  await people.getByTestId('agents.people.end').scrollIntoViewIfNeeded();
  await expect.poll(() => number(people, 'total'), { timeout: 30_000 }).toBeGreaterThanOrEqual(2 * WINDOW);
  await pageThrough(people, personRows, people.getByTestId('agents.people.more'), PEOPLE + 1);
  expect(await personRows.count()).toBeLessThanOrEqual(WINDOW);
  const from = await number(people, 'from');
  expect(from).toBeGreaterThan(0);
  await people.getByTestId('agents.people.earlier').click();
  await expect.poll(() => number(people, 'from')).toBeLessThan(from);
  // a seeded person's row near the top (pinned by owner: the end of the list coming into view moves the window)
  const earlier = people.getByTestId('agents.people.earlier');
  for (let clicks = 0; clicks < 20 && (await earlier.isVisible()); clicks += 1) await earlier.click();
  await page.evaluate(() => window.scrollTo(0, 0));
  const owner = (await personRows.nth(1).getAttribute('data-owner'))!;
  const other = people.locator(`[data-testid="agents.person"][data-owner="${owner}"]`);
  await other.getByTestId('agents.person.toggle').click();
  await expect(other.getByTestId('agents.item')).toHaveCount(1, { timeout: 30_000 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await quiet(page, `${label} people`);
}

test('stays small and usable with thousands of agents and hundreds of people on desktop', async ({ page, baseURL }) => {
  await scenario(page, baseURL!, 'desktop');
});

test.describe('phone', () => {
  const { defaultBrowserType: _browser, ...pixel } = devices['Pixel 7'];
  test.use(pixel);

  test('stays small and usable with thousands of agents and hundreds of people on a phone', async ({
    page,
    baseURL,
  }) => {
    await scenario(page, baseURL!, 'mobile');
  });
});
