// Volume gate (ADR-086 §4): the platform invitations (at the list cap) of a user with the platform role inviter.
import { test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/platform/invites', 'list:platform-invites'];

test('route:/platform/invites', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/platform/invites',
    path: '/platform/invites',
    ready: (page) => page.getByTestId('platform-invites.item').first(),
    primary: (page) => page.getByTestId('platform-invites.open'),
    also: ['list:platform-invites'],
  });
});
