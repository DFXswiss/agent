// Volume gate (ADR-086 §4): members (200 and more) and the org's invitations (at the list cap).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/members', 'list:members', 'list:members.invites'];

test('route:/o/:org/members', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/members',
    path: orgPath(volume.data, '/members'),
    ready: (page) => page.getByTestId('members.item').first(),
    primary: (page) => page.getByTestId('members.invite.open'),
    also: ['list:members', 'list:members.invites'],
  });
});
