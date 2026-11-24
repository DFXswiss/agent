// Volume gate (ADR-086 §4): live load – 20 sessions in other tasks of the workgroup write 200 events per second while
// a view is measured; the main thread may spend less than half of the time in long tasks, and the page must receive
// at least half of the events written meanwhile (`measure`).
import type { Page } from '@playwright/test';
import { orgPath, test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';

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
  // the center has no org of its own: it is reached from the open org (whose live channel keeps running), as a user
  // does with the bell – opened by a page load it would hold no live channel and the load would never reach it
  await volume.open(orgPath(volume.data, `/w/${volume.data.wg}`));
  const page = volume.page;
  await page.getByTestId('tasks.row').first().waitFor({ timeout: 60_000 });
  const since = Date.now();
  await page.getByTestId('bell.link').first().click();
  await measureHere(volume, info, since, {
    id: 'live:notifications',
    ready: (shown: Page) => shown.getByTestId('notifications.item').first(),
    primary: (shown: Page) => shown.getByTestId('notifications.mark-all'),
    live: true,
  });
});
