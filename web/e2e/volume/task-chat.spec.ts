// Volume gate (ADR-063 §4): the chat of a task with 10 000 items of every chat kind (prompts, messages, tool calls,
// files and file groups, denied hooks, status items, operation cards and results, subagent items) plus items at the
// event bound and very long lines; the scaling check against a task with 1 000 items per kind.
import { expect, orgPath, test } from './fixtures.ts';
import { BUDGET, type Measurement, record } from './budget.ts';
import { knownGap } from './gaps.ts';
import { openView } from './views.ts';

/** The chat kinds of `src/ui/chat-items/*.tsx` (each rendered in the measured window of the big task). */
const KINDS = [
  'countdown',
  'file-group',
  'operation.params',
  'operation.result',
  'prompt',
  'session.file',
  'session.hook',
  'session.output',
  'session.status',
  'session.tool',
] as const;

export const COVERS = [
  'route:/o/:org/t/:task',
  'tab:chat',
  'list:task.chat',
  'list:chat.files',
  'chat:countdown',
  'chat:file-group',
  'chat:operation.params',
  'chat:operation.result',
  'chat:prompt',
  'chat:session.file',
  'chat:session.hook',
  'chat:session.output',
  'chat:session.status',
  'chat:session.tool',
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

let small: Measurement | null = null;

test('scale:chat.1000', async ({ volume }, info) => {
  knownGap(info, 'scale:chat.1000');
  small = await openView(
    volume,
    info,
    chatView(orgPath(volume.data, `/t/${volume.data.small_task}`), 'scale:chat.1000'),
  );
});

test('route:/o/:org/t/:task', async ({ volume }, info) => {
  const big = await openView(
    volume,
    info,
    chatView(orgPath(volume.data, `/t/${volume.data.big_task}`), 'route:/o/:org/t/:task', [
      'tab:chat',
      'list:task.chat',
    ]),
  );
  // every chat kind is rendered in the measured view (the window holds the newest rows of every kind)
  const page = volume.page;
  const shown: string[] = [];
  for (const kind of KINDS) {
    const count = await page.locator(`[data-testid="chat.item"][data-kind="${kind}"]`).count();
    if (count > 0) shown.push(`chat:${kind}`);
  }
  if (shown.includes('chat:file-group')) shown.push('list:chat.files');
  if ((await page.getByTestId('chat.item.subagent').count()) > 0) shown.push('chat:subagent');
  record(big, shown);
  const missing = [...KINDS.map((kind) => `chat:${kind}`), 'chat:subagent'].filter((id) => !shown.includes(id));
  expect.soft(missing, 'chat kinds not rendered in the volume task').toEqual([]);
  // scaling: 10 times the items may cost at most twice as much (time until usable, heap)
  if (small) {
    const time = big.usableMs === null || small.usableMs === null ? Infinity : big.usableMs / small.usableMs;
    const heap = big.heapMb / Math.max(1, small.heapMb);
    console.log(
      `KOR-35 ${info.project.name} scale:chat: time factor ${time.toFixed(2)}, heap factor ${heap.toFixed(2)}`,
    );
    record({ ...big, id: 'scale:chat' });
    expect.soft(time, 'time factor 10 000 / 1 000 items per kind').toBeLessThanOrEqual(BUDGET.maxScalingFactor);
    expect.soft(heap, 'heap factor 10 000 / 1 000 items per kind').toBeLessThanOrEqual(BUDGET.maxScalingFactor);
  }
});
