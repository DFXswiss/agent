// Volume gate (ADR-086 §4): the chat of a task with 10 000 items of every chat kind (prompts, messages, tool calls,
// files and file groups, denied hooks, status items, operation cards and results, subagent items, work-VM jobs and
// states, test runs, browser sessions) plus items at the event bound and very long lines; the scaling check against a
// task with 1 000 items per kind.
import { expect, orgPath, test } from './fixtures.ts';
import { BUDGET, isShown, type Measurement, record } from './budget.ts';
import { knownGap, UNSEEDABLE } from './gaps.ts';
import { measureHere, openView } from './views.ts';

/** The chat kinds of `src/ui/chat-items/*.tsx` (each rendered in the measured window of the big task). */
const KINDS = [
  'browser.state',
  'countdown',
  'file-group',
  'operation.params',
  'operation.result',
  'prompt',
  'session.error',
  'session.file',
  'session.hook',
  'session.output',
  'session.status',
  'session.tool',
  'test.run',
  'workvm.run',
  'workvm.state',
] as const;

export const COVERS = [
  'route:/o/:org/t/:task',
  'tab:chat',
  'list:task.chat',
  'list:task.queue',
  'list:chat.files',
  'chat:browser.state',
  'chat:countdown',
  'chat:file-group',
  'chat:operation.params',
  'chat:operation.result',
  'chat:prompt',
  'chat:session.error',
  'chat:session.file',
  'chat:session.hook',
  'chat:session.output',
  'chat:session.status',
  'chat:session.tool',
  'chat:test.run',
  'chat:workvm.run',
  'chat:workvm.state',
  'chat:subagent',
  'scale:chat',
];

const chatView = (task: string, id: string, also: readonly string[] = []) => ({
  id,
  path: task,
  ready: (page: import('@playwright/test').Page) => page.getByTestId('chat.item').first(),
  primary: (page: import('@playwright/test').Page) => page.getByTestId('chat.composer.prompt'),
  also,
});

// the scaling check compares the two measurements above it: the file runs in one worker, in order
test.describe.configure({ mode: 'serial' });

let small: Measurement | null = null;
let bigResult: Measurement | null = null;

test('scale:chat.1000', async ({ volume }, info) => {
  test.skip(info.project.name.endsWith('-mobile'), 'the scaling check runs on the desktop (PHONE_EXEMPT, ADR-086 §4)');
  knownGap(info, 'scale:chat.1000');
  small = await openView(
    volume,
    info,
    chatView(orgPath(volume.data, `/t/${volume.data.small_task}`), 'scale:chat.1000'),
  );
});

test('route:/o/:org/t/:task', async ({ volume }, info) => {
  knownGap(info, 'route:/o/:org/t/:task');
  const view = chatView(orgPath(volume.data, `/t/${volume.data.big_task}`), 'route:/o/:org/t/:task', [
    'tab:chat',
    'list:task.chat',
    'list:task.queue',
  ]);
  const since = await volume.open(view.path);
  const page = volume.page;
  // every chat kind is rendered in the view as it opens (its first window holds the newest rows of every kind);
  // counted before `measure` moves the window
  const shown: string[] = [];
  let ownTime = 0;
  if (
    await view
      .ready(page)
      .waitFor({ timeout: 60_000 })
      .then(
        () => true,
        () => false,
      )
  ) {
    // the user's prompts of the newest page may arrive a moment after the session events (this wait and the counting
    // are the test's own time: left out of "usable")
    const waited = Date.now();
    await page
      .locator('[data-testid="chat.item"][data-kind="prompt"]')
      .first()
      .waitFor({ timeout: 15_000 })
      .catch(() => undefined);
    for (const kind of KINDS) {
      const count = await page.locator(`[data-testid="chat.item"][data-kind="${kind}"]`).count();
      if (count > 0) shown.push(`chat:${kind}`);
    }
    // a file group lists its files when opened; on slow devices the group may render a moment after the first
    // window and the list may expand a moment after the click, so wait for both instead of checking once
    const group = page.getByTestId('chat.files.toggle').first();
    const grouped = await group.waitFor({ timeout: 15_000 }).then(
      () => true,
      () => false,
    );
    if (grouped) await group.click({ timeout: 10_000 }).catch(() => undefined);
    await expect
      .poll(() => isShown(page, 'list:chat.files'), { timeout: 15_000 })
      .toBe(true)
      .catch(() => undefined);
    if (await isShown(page, 'list:chat.files')) shown.push('list:chat.files');
    if ((await page.getByTestId('chat.item.subagent').count()) > 0) shown.push('chat:subagent');
    ownTime = Date.now() - waited;
  }
  const big = await measureHere(volume, info, since + ownTime, view);
  // a kind not in the newest window must appear once the window moved to earlier rows
  if (big.elements >= 0) {
    for (const kind of KINDS) {
      if (shown.includes(`chat:${kind}`)) continue;
      const count = await page.locator(`[data-testid="chat.item"][data-kind="${kind}"]`).count();
      if (count > 0) shown.push(`chat:${kind}`);
    }
  }
  record(big, shown);
  const missing = [...KINDS.map((kind) => `chat:${kind}`), 'chat:subagent', 'list:chat.files'].filter(
    (id) => !shown.includes(id) && !(id in UNSEEDABLE),
  );
  expect.soft(missing, 'chat kinds not rendered in the volume task').toEqual([]);
  bigResult = big;
});

test('scale:chat', async ({ volume: _volume }, info) => {
  test.skip(info.project.name.endsWith('-mobile'), 'the scaling check runs on the desktop (PHONE_EXEMPT, ADR-086 §4)');
  // 10 times the items may cost at most twice the heap and three times the time until usable (or at most a second
  // more: sub-second times are mostly noise of the shared device); both are hard bounds (ADR-086 §4)
  expect(small, 'the 1 000-item chat was measured').not.toBeNull();
  expect(bigResult, 'the 10 000-item chat was measured').not.toBeNull();
  const [low, high] = [small!, bigResult!];
  const time = high.usableMs === null || low.usableMs === null ? Infinity : high.usableMs / low.usableMs;
  const extra = high.usableMs === null || low.usableMs === null ? Infinity : high.usableMs - low.usableMs;
  const heap = high.heapMb / Math.max(1, low.heapMb);
  console.log(
    `KOR-35 ${info.project.name} scale:chat: time factor ${time.toFixed(2)} (+${extra} ms), heap factor ${heap.toFixed(2)}`,
  );
  record({ ...high, id: 'scale:chat', violations: [] });
  expect
    .soft(
      time <= BUDGET.maxTimeScalingFactor || extra <= BUDGET.scalingSlackMs,
      `time factor ${time.toFixed(2)} (+${extra} ms) 10 000 / 1 000 items per kind`,
    )
    .toBe(true);
  expect.soft(heap, 'heap factor 10 000 / 1 000 items per kind').toBeLessThanOrEqual(BUDGET.maxScalingFactor);
});
