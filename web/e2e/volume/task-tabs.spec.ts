// Volume gate (ADR-063 §4): the other tabs of the volume task – its sessions (a subagent tree deeper and wider than
// the hub's limits), 10 000 operations and the detail of the largest result, its whiteboard (300 entries), its audit.
import type { Page } from '@playwright/test';
import { orgPath, test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';
import { knownGap } from './gaps.ts';

export const COVERS = [
  'tab:sessions',
  'list:task.sessions',
  'tab:operations',
  'list:task.operations',
  'view:operation.detail',
  'tab:whiteboard',
  'tab:audit',
  'list:task.audit',
];

const tab = (task: string, id: string) => `${task}?tab=${id}`;

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
    primary: (page) => page.getByTestId('operations.more'),
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
