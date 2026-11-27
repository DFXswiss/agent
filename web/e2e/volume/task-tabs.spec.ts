// Volume gate (ADR-086 §4): the other tabs of the volume task – "Claude Code" (the terminal of its main session), its sessions (a subagent tree deeper and wider than
// the hub's limits), 10 000 operations and the detail of the largest result, its whiteboard (300 entries), its audit, its 10 000 charges.
import type { Page } from '@playwright/test';
import { orgPath, test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';
import { knownGap } from './gaps.ts';

export const COVERS = [
  'tab:terminal',
  'tab:sessions',
  'list:task.sessions',
  'tab:operations',
  'list:task.operations',
  'view:operation.detail',
  'tab:whiteboard',
  'tab:audit',
  'list:task.audit',
  'tab:charges',
  'list:task.charges',
];

const tab = (task: string, id: string) => `${task}?tab=${id}`;

// the default tab "Claude Code" (KONZEPT E-43): the terminal of the main session with the prompt input below it
test('tab:terminal', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:terminal',
    path: orgPath(volume.data, `/t/${volume.data.big_task}`),
    ready: (page) => page.getByTestId('task.terminal'),
    primary: (page) => page.getByTestId('chat.composer.prompt'),
  });
});

test('tab:sessions', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:sessions',
    path: tab(orgPath(volume.data, `/t/${volume.data.big_task}`), 'sessions'),
    ready: (page) => page.getByTestId('sessions.node').first(),
    primary: (page) => page.getByTestId('sessions.link').first(),
    also: ['list:task.sessions'],
  });
});

test('tab:operations', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:operations',
    path: tab(orgPath(volume.data, `/t/${volume.data.big_task}`), 'operations'),
    ready: (page) => page.getByTestId('operations.item').first(),
    primary: (page) => page.getByTestId('operations.open').first(),
    also: ['list:task.operations'],
  });
});

test('view:operation.detail', async ({ volume }, info) => {
  knownGap(info, 'view:operation.detail');
  const task = orgPath(volume.data, `/t/${volume.data.big_task}`);
  const since = await volume.open(`${task}?tab=operations&op=${volume.data.big_operation}`);
  await measureHere(volume, info, since, {
    id: 'view:operation.detail',
    ready: (page: Page) => page.getByTestId('operations.detail.result'),
    primary: (page: Page) => page.getByTestId('operations.detail.close'),
    scroll: (page: Page) => page.getByTestId('operations.detail'),
  });
});

test('tab:whiteboard', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:whiteboard',
    path: tab(orgPath(volume.data, `/t/${volume.data.big_task}`), 'whiteboard'),
    ready: (page) => page.getByTestId('whiteboard.item').first(),
    primary: (page) => page.getByTestId('whiteboard.create'),
  });
});

test('tab:audit', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:audit',
    path: tab(orgPath(volume.data, `/t/${volume.data.big_task}`), 'audit'),
    ready: (page) => page.getByTestId('audit.row').first(),
    primary: (page) => page.getByTestId('audit.filter.apply'),
    also: ['list:task.audit'],
  });
});

test('tab:charges', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:charges',
    path: tab(orgPath(volume.data, `/t/${volume.data.big_task}`), 'charges'),
    ready: (page) => page.getByTestId('charges.sessions'),
    primary: (page) => page.getByTestId('charges.reload'),
    also: ['list:task.charges'],
  });
});
