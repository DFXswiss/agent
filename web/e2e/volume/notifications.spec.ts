// Volume gate (ADR-063 §4): 2 000 notifications of the signed-in user.
import { test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/notifications', 'list:notifications'];

test('route:/notifications', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/notifications',
    path: '/notifications',
    ready: (page) => page.getByTestId('notifications.item').first(),
    primary: (page) => page.getByTestId('notifications.more'),
    also: ['list:notifications'],
  });
});
