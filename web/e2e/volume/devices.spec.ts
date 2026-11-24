// Volume gate (ADR-086 §4): the org's devices (150 org devices and the platform device) and device invitations.
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/devices', 'list:devices', 'list:devices.invites'];

test('route:/o/:org/devices', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/devices',
    path: orgPath(volume.data, '/devices'),
    ready: (page) => page.getByTestId('devices.item').first(),
    primary: (page) => page.getByTestId('devices.add.open'),
    also: ['list:devices', 'list:devices.invites'],
  });
});
