// Volume gate (ADR-063 §4): live load – 20 sessions in other tasks of the workgroup write 200 events per second while
// a view is measured; the main thread may spend less than half of the time in long tasks.
import type { Page } from '@playwright/test';
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['live:task.chat', 'live:tasks.list', 'live:whiteboard', 'live:notifications'];

test('live:task.chat', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'live:task.chat',
    path: orgPath(volume.data, `/t/${volume.data.small_task}`),
    ready: (page: Page) => page.getByTestId('chat.item').first(),
    primary: (page: Page) => page.getByTestId('chat.composer.prompt'),
    live: true,
  });
});

test('live:tasks.list', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'live:tasks.list',
    path: orgPath(volume.data, `/w/${volume.data.wg}`),
    ready: (page: Page) => page.getByTestId('tasks.row').first(),
    primary: (page: Page) => page.getByTestId('composer.prompt'),
    live: true,
  });
});

test('live:whiteboard', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'live:whiteboard',
    path: orgPath(volume.data, `/w/${volume.data.wg}/whiteboard`),
    ready: (page: Page) => page.getByTestId('whiteboard.item').first(),
    primary: (page: Page) => page.getByTestId('whiteboard.create'),
    live: true,
  });
});

test('live:notifications', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'live:notifications',
    path: '/notifications',
    ready: (page: Page) => page.getByTestId('notifications.item').first(),
    primary: (page: Page) => page.getByTestId('notifications.more'),
    live: true,
  });
});
